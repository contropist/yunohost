# -*- coding: utf-8 -*-

""" License

    Copyright (C) 2018 YUNOHOST.ORG

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as published
    by the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program; if not, see http://www.gnu.org/licenses

"""

import os
import re
import urllib.parse
import tempfile
import shutil
from collections import OrderedDict
from typing import Optional, Dict, List, Union, Any, Mapping

from moulinette.interfaces.cli import colorize
from moulinette import Moulinette, m18n
from moulinette.utils.log import getActionLogger
from moulinette.utils.filesystem import (
    read_file,
    write_to_file,
    read_toml,
    read_yaml,
    write_to_yaml,
    mkdir,
)

from yunohost.utils.i18n import _value_for_locale
from yunohost.utils.error import YunohostError, YunohostValidationError
from yunohost.log import OperationLogger

logger = getActionLogger("yunohost.config")
CONFIG_PANEL_VERSION_SUPPORTED = 1.0


class ConfigPanel:
    def __init__(self, config_path, save_path=None):
        self.config_path = config_path
        self.save_path = save_path
        self.config = {}
        self.values = {}
        self.new_values = {}

    def get(self, key="", mode="classic"):
        self.filter_key = key or ""

        # Read config panel toml
        self._get_config_panel()

        if not self.config:
            raise YunohostValidationError("config_no_panel")

        # Read or get values and hydrate the config
        self._load_current_values()
        self._hydrate()

        # In 'classic' mode, we display the current value if key refer to an option
        if self.filter_key.count(".") == 2 and mode == "classic":
            option = self.filter_key.split(".")[-1]
            return self.values.get(option, None)

        # Format result in 'classic' or 'export' mode
        logger.debug(f"Formating result in '{mode}' mode")
        result = {}
        for panel, section, option in self._iterate():
            key = f"{panel['id']}.{section['id']}.{option['id']}"
            if mode == "export":
                result[option["id"]] = option.get("current_value")
                continue

            ask = None
            if "ask" in option:
                ask = _value_for_locale(option["ask"])
            elif "i18n" in self.config:
                ask = m18n.n(self.config["i18n"] + "_" + option["id"])

            if mode == "full":
                # edit self.config directly
                option["ask"] = ask
            else:
                result[key] = {"ask": ask}
                if "current_value" in option:
                    question_class = ARGUMENTS_TYPE_PARSERS[
                        option.get("type", "string")
                    ]
                    result[key]["value"] = question_class.humanize(
                        option["current_value"], option
                    )
                    # FIXME: semantics, technically here this is not about a prompt...
                    if question_class.hide_user_input_in_prompt:
                        result[key]["value"] = "**************"  # Prevent displaying password in `config get`

        if mode == "full":
            return self.config
        else:
            return result

    def set(
        self, key=None, value=None, args=None, args_file=None, operation_logger=None
    ):
        self.filter_key = key or ""

        # Read config panel toml
        self._get_config_panel()

        if not self.config:
            raise YunohostValidationError("config_no_panel")

        if (args is not None or args_file is not None) and value is not None:
            raise YunohostValidationError(
                "You should either provide a value, or a serie of args/args_file, but not both at the same time",
                raw_msg=True,
            )

        if self.filter_key.count(".") != 2 and value is not None:
            raise YunohostValidationError("config_cant_set_value_on_section")

        # Import and parse pre-answered options
        logger.debug("Import and parse pre-answered options")
        args = urllib.parse.parse_qs(args or "", keep_blank_values=True)
        self.args = {key: ",".join(value_) for key, value_ in args.items()}

        if args_file:
            # Import YAML / JSON file but keep --args values
            self.args = {**read_yaml(args_file), **self.args}

        if value is not None:
            self.args = {self.filter_key.split(".")[-1]: value}

        # Read or get values and hydrate the config
        self._load_current_values()
        self._hydrate()
        self._ask()

        if operation_logger:
            operation_logger.start()

        try:
            self._apply()
        except YunohostError:
            raise
        # Script got manually interrupted ...
        # N.B. : KeyboardInterrupt does not inherit from Exception
        except (KeyboardInterrupt, EOFError):
            error = m18n.n("operation_interrupted")
            logger.error(m18n.n("config_apply_failed", error=error))
            raise
        # Something wrong happened in Yunohost's code (most probably hook_exec)
        except Exception:
            import traceback

            error = m18n.n("unexpected_error", error="\n" + traceback.format_exc())
            logger.error(m18n.n("config_apply_failed", error=error))
            raise
        finally:
            # Delete files uploaded from API
            # FIXME : this is currently done in the context of config panels,
            # but could also happen in the context of app install ... (or anywhere else
            # where we may parse args etc...)
            FileQuestion.clean_upload_dirs()

        self._reload_services()

        logger.success("Config updated as expected")
        operation_logger.success()

    def _get_toml(self):
        return read_toml(self.config_path)

    def _get_config_panel(self):

        # Split filter_key
        filter_key = self.filter_key.split(".") if self.filter_key != "" else []
        if len(filter_key) > 3:
            raise YunohostError(
                f"The filter key {filter_key} has too many sub-levels, the max is 3.",
                raw_msg=True,
            )

        if not os.path.exists(self.config_path):
            logger.debug(f"Config panel {self.config_path} doesn't exists")
            return None

        toml_config_panel = self._get_toml()

        # Check TOML config panel is in a supported version
        if float(toml_config_panel["version"]) < CONFIG_PANEL_VERSION_SUPPORTED:
            raise YunohostError(
                "config_version_not_supported", version=toml_config_panel["version"]
            )

        # Transform toml format into internal format
        format_description = {
            "root": {
                "properties": ["version", "i18n"],
                "defaults": {"version": 1.0},
            },
            "panels": {
                "properties": ["name", "services", "actions", "help"],
                "defaults": {
                    "services": [],
                    "actions": {"apply": {"en": "Apply"}},
                },
            },
            "sections": {
                "properties": ["name", "services", "optional", "help", "visible"],
                "defaults": {
                    "name": "",
                    "services": [],
                    "optional": True,
                },
            },
            "options": {
                "properties": [
                    "ask",
                    "type",
                    "bind",
                    "help",
                    "example",
                    "default",
                    "style",
                    "icon",
                    "placeholder",
                    "visible",
                    "optional",
                    "choices",
                    "yes",
                    "no",
                    "pattern",
                    "limit",
                    "min",
                    "max",
                    "step",
                    "accept",
                    "redact",
                ],
                "defaults": {},
            },
        }

        def _build_internal_config_panel(raw_infos, level):
            """Convert TOML in internal format ('full' mode used by webadmin)
            Here are some properties of 1.0 config panel in toml:
            - node properties and node children are mixed,
            - text are in english only
            - some properties have default values
            This function detects all children nodes and put them in a list
            """

            defaults = format_description[level]["defaults"]
            properties = format_description[level]["properties"]

            # Start building the ouput (merging the raw infos + defaults)
            out = {key: raw_infos.get(key, value) for key, value in defaults.items()}

            # Now fill the sublevels (+ apply filter_key)
            i = list(format_description).index(level)
            sublevel = (
                list(format_description)[i + 1] if level != "options" else None
            )
            search_key = filter_key[i] if len(filter_key) > i else False

            for key, value in raw_infos.items():
                # Key/value are a child node
                if (
                    isinstance(value, OrderedDict)
                    and key not in properties
                    and sublevel
                ):
                    # We exclude all nodes not referenced by the filter_key
                    if search_key and key != search_key:
                        continue
                    subnode = _build_internal_config_panel(value, sublevel)
                    subnode["id"] = key
                    if level == "root":
                        subnode.setdefault("name", {"en": key.capitalize()})
                    elif level == "sections":
                        subnode["name"] = key  # legacy
                        subnode.setdefault("optional", raw_infos.get("optional", True))
                    out.setdefault(sublevel, []).append(subnode)
                # Key/value are a property
                else:
                    if key not in properties:
                        logger.warning(f"Unknown key '{key}' found in config panel")
                    # Todo search all i18n keys
                    out[key] = (
                        value if key not in ["ask", "help", "name"] else {"en": value}
                    )
            return out

        self.config = _build_internal_config_panel(toml_config_panel, "root")

        try:
            self.config["panels"][0]["sections"][0]["options"][0]
        except (KeyError, IndexError):
            raise YunohostValidationError(
                "config_unknown_filter_key", filter_key=self.filter_key
            )

        # List forbidden keywords from helpers and sections toml (to avoid conflict)
        forbidden_keywords = [
            "old",
            "app",
            "changed",
            "file_hash",
            "binds",
            "types",
            "formats",
            "getter",
            "setter",
            "short_setting",
            "type",
            "bind",
            "nothing_changed",
            "changes_validated",
            "result",
            "max_progression",
        ]
        forbidden_keywords += format_description["sections"]

        for _, _, option in self._iterate():
            if option["id"] in forbidden_keywords:
                raise YunohostError("config_forbidden_keyword", keyword=option["id"])
        return self.config

    def _hydrate(self):
        # Hydrating config panel with current value
        logger.debug("Hydrating config with current values")
        for _, _, option in self._iterate():
            if option["id"] not in self.values:
                allowed_empty_types = ["alert", "display_text", "markdown", "file"]
                if (
                    option["type"] in allowed_empty_types
                    or option.get("bind") == "null"
                ):
                    continue
                else:
                    raise YunohostError(
                        f"Config panel question '{option['id']}' should be initialized with a value during install or upgrade.",
                        raw_msg=True,
                    )
            value = self.values[option["name"]]
            # In general, the value is just a simple value.
            # Sometimes it could be a dict used to overwrite the option itself
            value = value if isinstance(value, dict) else {"current_value": value}
            option.update(value)

        return self.values

    def _ask(self):
        logger.debug("Ask unanswered question and prevalidate data")

        if "i18n" in self.config:
            for panel, section, option in self._iterate():
                if "ask" not in option:
                    option["ask"] = m18n.n(self.config["i18n"] + "_" + option["id"])

        def display_header(message):
            """CLI panel/section header display"""
            if Moulinette.interface.type == "cli" and self.filter_key.count(".") < 2:
                Moulinette.display(colorize(message, "purple"))

        for panel, section, obj in self._iterate(["panel", "section"]):
            if panel == obj:
                name = _value_for_locale(panel["name"])
                display_header(f"\n{'='*40}\n>>>> {name}\n{'='*40}")
                continue
            name = _value_for_locale(section["name"])
            if name:
                display_header(f"\n# {name}")

            # Check and ask unanswered questions
            questions = ask_questions_and_parse_answers(section["options"], self.args)
            self.new_values.update({
                question.name: question.value
                for question in questions
                if question.value is not None
            })

        self.errors = None

    def _get_default_values(self):
        return {
            option["id"]: option["default"]
            for _, _, option in self._iterate()
            if "default" in option
        }

    def _load_current_values(self):
        """
        Retrieve entries in YAML file
        And set default values if needed
        """

        # Retrieve entries in the YAML
        on_disk_settings = {}
        if os.path.exists(self.save_path) and os.path.isfile(self.save_path):
            on_disk_settings = read_yaml(self.save_path) or {}

        # Inject defaults if needed (using the magic .update() ;))
        self.values = self._get_default_values()
        self.values.update(on_disk_settings)

    def _apply(self):
        logger.info("Saving the new configuration...")
        dir_path = os.path.dirname(os.path.realpath(self.save_path))
        if not os.path.exists(dir_path):
            mkdir(dir_path, mode=0o700)

        values_to_save = {**self.values, **self.new_values}
        if self.save_mode == "diff":
            defaults = self._get_default_values()
            values_to_save = {
                k: v for k, v in values_to_save.items() if defaults.get(k) != v
            }

        # Save the settings to the .yaml file
        write_to_yaml(self.save_path, values_to_save)

    def _reload_services(self):

        from yunohost.service import service_reload_or_restart

        services_to_reload = set()
        for panel, section, obj in self._iterate(["panel", "section", "option"]):
            services_to_reload |= set(obj.get("services", []))

        services_to_reload = list(services_to_reload)
        services_to_reload.sort(key="nginx".__eq__)
        if services_to_reload:
            logger.info("Reloading services...")
        for service in services_to_reload:
            if hasattr(self, "app"):
                service = service.replace("__APP__", self.app)
            service_reload_or_restart(service)

    def _iterate(self, trigger=["option"]):
        for panel in self.config.get("panels", []):
            if "panel" in trigger:
                yield (panel, None, panel)
            for section in panel.get("sections", []):
                if "section" in trigger:
                    yield (panel, section, section)
                if "option" in trigger:
                    for option in section.get("options", []):
                        yield (panel, section, option)


class Question(object):
    hide_user_input_in_prompt = False
    pattern: Optional[Dict] = None

    def __init__(self, question: Dict[str, Any]):
        self.name = question["name"]
        self.type = question.get("type", "string")
        self.default = question.get("default", None)
        self.optional = question.get("optional", False)
        self.choices = question.get("choices", [])
        self.pattern = question.get("pattern", self.pattern)
        self.ask = question.get("ask", {"en": self.name})
        self.help = question.get("help")
        self.redact = question.get("redact", False)
        # .current_value is the currently stored value
        self.current_value = question.get("current_value")
        # .value is the "proposed" value which we got from the user
        self.value = question.get("value")

        # Empty value is parsed as empty string
        if self.default == "":
            self.default = None

    @staticmethod
    def humanize(value, option={}):
        return str(value)

    @staticmethod
    def normalize(value, option={}):
        if isinstance(value, str):
            value = value.strip()
        return value

    def _prompt(self, text):
        prefill = ""
        if self.current_value is not None:
            prefill = self.humanize(self.current_value, self)
        elif self.default is not None:
            prefill = self.humanize(self.default, self)
        self.value = Moulinette.prompt(
            message=text,
            is_password=self.hide_user_input_in_prompt,
            confirm=False,
            prefill=prefill,
            is_multiline=(self.type == "text"),
            autocomplete=self.choices,
            help=_value_for_locale(self.help)
        )

    def ask_if_needed(self):
        for i in range(5):
            # Display question if no value filled or if it's a readonly message
            if Moulinette.interface.type == "cli" and os.isatty(1):
                text_for_user_input_in_cli = self._format_text_for_user_input_in_cli()
                if getattr(self, "readonly", False):
                    Moulinette.display(text_for_user_input_in_cli)
                elif self.value is None:
                    self._prompt(text_for_user_input_in_cli)

            # Apply default value
            class_default = getattr(self, "default_value", None)
            if self.value in [None, ""] and (
                self.default is not None or class_default is not None
            ):
                self.value = class_default if self.default is None else self.default

            try:
                # Normalize and validate
                self.value = self.normalize(self.value, self)
                self._prevalidate()
            except YunohostValidationError as e:
                # If in interactive cli, re-ask the current question
                if i < 4 and Moulinette.interface.type == "cli" and os.isatty(1):
                    logger.error(str(e))
                    self.value = None
                    continue

                # Otherwise raise the ValidationError
                raise

            break

        self.value = self._post_parse_value()

        return self.value

    def _prevalidate(self):
        if self.value in [None, ""] and not self.optional:
            raise YunohostValidationError("app_argument_required", name=self.name)

        # we have an answer, do some post checks
        if self.value not in [None, ""]:
            if self.choices and self.value not in self.choices:
                raise YunohostValidationError(
                    "app_argument_choice_invalid",
                    name=self.name,
                    value=self.value,
                    choices=", ".join(self.choices),
                )
            if self.pattern and not re.match(self.pattern["regexp"], str(self.value)):
                raise YunohostValidationError(
                    self.pattern["error"],
                    name=self.name,
                    value=self.value,
                )

    def _format_text_for_user_input_in_cli(self):

        text_for_user_input_in_cli = _value_for_locale(self.ask)

        if self.choices:

            # Prevent displaying a shitload of choices
            # (e.g. 100+ available users when choosing an app admin...)
            choices = list(self.choices.values()) if isinstance(self.choices, dict) else self.choices
            choices_to_display = choices[:20]
            remaining_choices = len(choices[20:])

            if remaining_choices > 0:
                choices_to_display += [m18n.n("other_available_options", n=remaining_choices)]

            choices_to_display = " | ".join(choices_to_display)

            text_for_user_input_in_cli += f" [{choices_to_display}]"

        return text_for_user_input_in_cli

    def _post_parse_value(self):
        if not self.redact:
            return self.value

        # Tell the operation_logger to redact all password-type / secret args
        # Also redact the % escaped version of the password that might appear in
        # the 'args' section of metadata (relevant for password with non-alphanumeric char)
        data_to_redact = []
        if self.value and isinstance(self.value, str):
            data_to_redact.append(self.value)
        if self.current_value and isinstance(self.current_value, str):
            data_to_redact.append(self.current_value)
        data_to_redact += [
            urllib.parse.quote(data)
            for data in data_to_redact
            if urllib.parse.quote(data) != data
        ]

        for operation_logger in OperationLogger._instances:
            operation_logger.data_to_redact.extend(data_to_redact)

        return self.value


class StringQuestion(Question):
    argument_type = "string"
    default_value = ""


class EmailQuestion(StringQuestion):
    pattern = {
        "regexp": r"^.+@.+",
        "error": "config_validate_email",  # i18n: config_validate_email
    }


class URLQuestion(StringQuestion):
    pattern = {
        "regexp": r"^https?://.*$",
        "error": "config_validate_url",  # i18n: config_validate_url
    }


class DateQuestion(StringQuestion):
    pattern = {
        "regexp": r"^\d{4}-\d\d-\d\d$",
        "error": "config_validate_date",  # i18n: config_validate_date
    }

    def _prevalidate(self):
        from datetime import datetime

        super()._prevalidate()

        if self.value not in [None, ""]:
            try:
                datetime.strptime(self.value, "%Y-%m-%d")
            except ValueError:
                raise YunohostValidationError("config_validate_date")


class TimeQuestion(StringQuestion):
    pattern = {
        "regexp": r"^(1[12]|0?\d):[0-5]\d$",
        "error": "config_validate_time",  # i18n: config_validate_time
    }


class ColorQuestion(StringQuestion):
    pattern = {
        "regexp": r"^#[ABCDEFabcdef\d]{3,6}$",
        "error": "config_validate_color",  # i18n: config_validate_color
    }


class TagsQuestion(Question):
    argument_type = "tags"

    @staticmethod
    def humanize(value, option={}):
        if isinstance(value, list):
            return ",".join(value)
        return value

    @staticmethod
    def normalize(value, option={}):
        if isinstance(value, list):
            return ",".join(value)
        if isinstance(value, str):
            value = value.strip()
        return value

    def _prevalidate(self):
        values = self.value
        if isinstance(values, str):
            values = values.split(",")
        elif values is None:
            values = []
        for value in values:
            self.value = value
            super()._prevalidate()
        self.value = values

    def _post_parse_value(self):
        if isinstance(self.value, list):
            self.value = ",".join(self.value)
        return super()._post_parse_value()


class PasswordQuestion(Question):
    hide_user_input_in_prompt = True
    argument_type = "password"
    default_value = ""
    forbidden_chars = "{}"

    def __init__(self, question):
        super().__init__(question)
        self.redact = True
        if self.default is not None:
            raise YunohostValidationError(
                "app_argument_password_no_default", name=self.name
            )

    def _prevalidate(self):
        super()._prevalidate()

        if self.value not in [None, ""]:
            if any(char in self.value for char in self.forbidden_chars):
                raise YunohostValidationError(
                    "pattern_password_app", forbidden_chars=self.forbidden_chars
                )

            # If it's an optional argument the value should be empty or strong enough
            from yunohost.utils.password import assert_password_is_strong_enough

            assert_password_is_strong_enough("user", self.value)


class PathQuestion(Question):
    argument_type = "path"
    default_value = ""

    @staticmethod
    def normalize(value, option={}):

        option = option.__dict__ if isinstance(option, Question) else option

        if not value.strip():
            if option.get("optional"):
                return ""
            # Hmpf here we could just have a "else" case
            # but we also want PathQuestion.normalize("") to return "/"
            # (i.e. if no option is provided, hence .get("optional") is None
            elif option.get("optional") is False:
                raise YunohostValidationError(
                    "app_argument_invalid",
                    name=option.get("name"),
                    error="Question is mandatory"
                )

        return "/" + value.strip().strip(" /")


class BooleanQuestion(Question):
    argument_type = "boolean"
    default_value = 0
    yes_answers = ["1", "yes", "y", "true", "t", "on"]
    no_answers = ["0", "no", "n", "false", "f", "off"]

    @staticmethod
    def humanize(value, option={}):

        option = option.__dict__ if isinstance(option, Question) else option

        yes = option.get("yes", 1)
        no = option.get("no", 0)

        value = BooleanQuestion.normalize(value, option)

        if value == yes:
            return "yes"
        if value == no:
            return "no"
        if value is None:
            return ""

        raise YunohostValidationError(
            "app_argument_choice_invalid",
            name=option.get("name"),
            value=value,
            choices="yes/no",
        )

    @staticmethod
    def normalize(value, option={}):

        option = option.__dict__ if isinstance(option, Question) else option

        if isinstance(value, str):
            value = value.strip()

        technical_yes = option.get("yes", 1)
        technical_no = option.get("no", 0)

        no_answers = BooleanQuestion.no_answers
        yes_answers = BooleanQuestion.yes_answers

        assert str(technical_yes).lower() not in no_answers, f"'yes' value can't be in {no_answers}"
        assert str(technical_no).lower() not in yes_answers, f"'no' value can't be in {yes_answers}"

        no_answers += [str(technical_no).lower()]
        yes_answers += [str(technical_yes).lower()]

        strvalue = str(value).lower()

        if strvalue in yes_answers:
            return technical_yes
        if strvalue in no_answers:
            return technical_no

        if strvalue in ["none", ""]:
            return None

        raise YunohostValidationError(
            "app_argument_choice_invalid",
            name=option.get("name"),
            value=strvalue,
            choices="yes/no",
        )

    def __init__(self, question):
        super().__init__(question)
        self.yes = question.get("yes", 1)
        self.no = question.get("no", 0)
        if self.default is None:
            self.default = self.no

    def _format_text_for_user_input_in_cli(self):
        text_for_user_input_in_cli = super()._format_text_for_user_input_in_cli()

        text_for_user_input_in_cli += " [yes | no]"

        return text_for_user_input_in_cli

    def get(self, key, default=None):
        return getattr(self, key, default)


class DomainQuestion(Question):
    argument_type = "domain"

    def __init__(self, question):
        from yunohost.domain import domain_list, _get_maindomain

        super().__init__(question)

        if self.default is None:
            self.default = _get_maindomain()

        self.choices = domain_list()["domains"]

    @staticmethod
    def normalize(value, option={}):
        if value.startswith("https://"):
            value = value[len("https://"):]
        elif value.startswith("http://"):
            value = value[len("http://"):]

        # Remove trailing slashes
        value = value.rstrip("/").lower()

        return value


class UserQuestion(Question):
    argument_type = "user"

    def __init__(self, question):
        from yunohost.user import user_list, user_info
        from yunohost.domain import _get_maindomain

        super().__init__(question)
        self.choices = list(user_list()["users"].keys())

        if not self.choices:
            raise YunohostValidationError(
                "app_argument_invalid",
                name=self.name,
                error="You should create a YunoHost user first.",
            )

        if self.default is None:
            root_mail = "root@%s" % _get_maindomain()
            for user in self.choices:
                if root_mail in user_info(user).get("mail-aliases", []):
                    self.default = user
                    break


class NumberQuestion(Question):
    argument_type = "number"
    default_value = None

    def __init__(self, question):
        super().__init__(question)
        self.min = question.get("min", None)
        self.max = question.get("max", None)
        self.step = question.get("step", None)

    @staticmethod
    def normalize(value, option={}):

        if isinstance(value, int):
            return value

        if isinstance(value, str):
            value = value.strip()

        if isinstance(value, str) and value.isdigit():
            return int(value)

        if value in [None, ""]:
            return value

        option = option.__dict__ if isinstance(option, Question) else option
        raise YunohostValidationError(
            "app_argument_invalid",
            name=option.get("name"),
            error=m18n.n("invalid_number")
        )

    def _prevalidate(self):
        super()._prevalidate()
        if self.value in [None, ""]:
            return

        if self.min is not None and int(self.value) < self.min:
            raise YunohostValidationError(
                "app_argument_invalid",
                name=self.name,
                error=m18n.n("invalid_number_min", min=self.min),
            )

        if self.max is not None and int(self.value) > self.max:
            raise YunohostValidationError(
                "app_argument_invalid",
                name=self.name,
                error=m18n.n("invalid_number_max", max=self.max),
            )


class DisplayTextQuestion(Question):
    argument_type = "display_text"
    readonly = True

    def __init__(self, question):
        super().__init__(question)

        self.optional = True
        self.style = question.get(
            "style", "info" if question["type"] == "alert" else ""
        )

    def _format_text_for_user_input_in_cli(self):
        text = _value_for_locale(self.ask)

        if self.style in ["success", "info", "warning", "danger"]:
            color = {
                "success": "green",
                "info": "cyan",
                "warning": "yellow",
                "danger": "red",
            }
            prompt = m18n.g(self.style) if self.style != "danger" else m18n.n("danger")
            return colorize(prompt, color[self.style]) + f" {text}"
        else:
            return text


class FileQuestion(Question):
    argument_type = "file"
    upload_dirs: List[str] = []

    @classmethod
    def clean_upload_dirs(cls):
        # Delete files uploaded from API
        for upload_dir in cls.upload_dirs:
            if os.path.exists(upload_dir):
                shutil.rmtree(upload_dir)

    def __init__(self, question):
        super().__init__(question)
        self.accept = question.get("accept", "")

    def _prevalidate(self):
        if self.value is None:
            self.value = self.current_value

        super()._prevalidate()

        if Moulinette.interface.type != "api":
            if not self.value or not os.path.exists(str(self.value)):
                raise YunohostValidationError(
                    "app_argument_invalid",
                    name=self.name,
                    error=m18n.n("file_does_not_exist", path=str(self.value)),
                )

    def _post_parse_value(self):
        from base64 import b64decode

        if not self.value:
            return self.value

        upload_dir = tempfile.mkdtemp(prefix="ynh_filequestion_")
        _, file_path = tempfile.mkstemp(dir=upload_dir)

        FileQuestion.upload_dirs += [upload_dir]

        logger.debug(f"Saving file {self.name} for file question into {file_path}")
        if Moulinette.interface.type != "api":
            content = read_file(str(self.value), file_mode="rb")

        if Moulinette.interface.type == "api":
            content = b64decode(self.value)

        write_to_file(file_path, content, file_mode="wb")

        self.value = file_path

        return self.value


ARGUMENTS_TYPE_PARSERS = {
    "string": StringQuestion,
    "text": StringQuestion,
    "select": StringQuestion,
    "tags": TagsQuestion,
    "email": EmailQuestion,
    "url": URLQuestion,
    "date": DateQuestion,
    "time": TimeQuestion,
    "color": ColorQuestion,
    "password": PasswordQuestion,
    "path": PathQuestion,
    "boolean": BooleanQuestion,
    "domain": DomainQuestion,
    "user": UserQuestion,
    "number": NumberQuestion,
    "range": NumberQuestion,
    "display_text": DisplayTextQuestion,
    "alert": DisplayTextQuestion,
    "markdown": DisplayTextQuestion,
    "file": FileQuestion,
}


def ask_questions_and_parse_answers(questions: Dict, prefilled_answers: Union[str, Mapping[str, Any]] = {}) -> List[Question]:
    """Parse arguments store in either manifest.json or actions.json or from a
    config panel against the user answers when they are present.

    Keyword arguments:
        questions         -- the arguments description store in yunohost
                              format from actions.json/toml, manifest.json/toml
                              or config_panel.json/toml
        prefilled_answers -- a url "query-string" such as "domain=yolo.test&path=/foobar&admin=sam"
                             or a dict such as {"domain": "yolo.test", "path": "/foobar", "admin": "sam"}
    """

    if isinstance(prefilled_answers, str):
        # FIXME FIXME : this is not uniform with config_set() which uses parse.qs (no l)
        # parse_qsl parse single values
        # whereas parse.qs return list of values (which is useful for tags, etc)
        # For now, let's not migrate this piece of code to parse_qs
        # Because Aleks believes some bits of the app CI rely on overriding values (e.g. foo=foo&...&foo=bar)
        prefilled_answers = dict(urllib.parse.parse_qsl(prefilled_answers or "", keep_blank_values=True))

    if not prefilled_answers:
        prefilled_answers = {}

    out = []

    for question in questions:
        question_class = ARGUMENTS_TYPE_PARSERS[question.get("type", "string")]
        question["value"] = prefilled_answers.get(question["name"])
        question = question_class(question)

        question.ask_if_needed()
        out.append(question)

    return out