#
# Copyright (c) 2024 YunoHost Contributors
#
# This file is part of YunoHost (see https://yunohost.org)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
import os
import re
import glob
from logging import getLogger

from moulinette.core import MoulinetteError
from moulinette.utils.filesystem import (
    read_file,
    write_to_file,
    write_to_yaml,
    read_yaml,
)

from yunohost.utils.error import YunohostValidationError


logger = getLogger("yunohost.utils.legacy")

LEGACY_PHP_VERSION_REPLACEMENTS = [
    ("/etc/php5", "/etc/php/8.2"),
    ("/etc/php/7.0", "/etc/php/8.2"),
    ("/etc/php/7.3", "/etc/php/8.2"),
    ("/etc/php/7.4", "/etc/php/8.2"),
    ("/var/run/php5-fpm", "/var/run/php/php8.2-fpm"),
    ("/var/run/php/php7.0-fpm", "/var/run/php/php8.2-fpm"),
    ("/var/run/php/php7.3-fpm", "/var/run/php/php8.2-fpm"),
    ("/var/run/php/php7.4-fpm", "/var/run/php/php8.2-fpm"),
    ("php5", "php8.2"),
    ("php7.0", "php8.2"),
    ("php7.3", "php8.2"),
    ("php7.4", "php8.2"),
    ('YNH_PHP_VERSION="7.3"', 'YNH_PHP_VERSION="8.2"'),
    ('YNH_PHP_VERSION="7.4"', 'YNH_PHP_VERSION="8.2"'),
    (
        'phpversion="${phpversion:-7.0}"',
        'phpversion="${phpversion:-8.2}"',
    ),  # Many helpers like the composer ones use 7.0 by default ...
    (
        'phpversion="${phpversion:-7.3}"',
        'phpversion="${phpversion:-8.2}"',
    ),  # Many helpers like the composer ones use 7.0 by default ...
    (
        'phpversion="${phpversion:-7.4}"',
        'phpversion="${phpversion:-8.2}"',
    ),  # Many helpers like the composer ones use 7.0 by default ...
    (
        '"$phpversion" == "7.0"',
        '$(bc <<< "$phpversion >= 8.2") -eq 1',
    ),  # patch ynh_install_php to refuse installing/removing php <= 7.3
    (
        '"$phpversion" == "7.3"',
        '$(bc <<< "$phpversion >= 8.2") -eq 1',
    ),  # patch ynh_install_php to refuse installing/removing php <= 7.3
    (
        '"$phpversion" == "7.4"',
        '$(bc <<< "$phpversion >= 8.2") -eq 1',
    ),  # patch ynh_install_php to refuse installing/removing php <= 7.3

]


def _patch_legacy_php_versions(app_folder):
    files_to_patch = []
    files_to_patch.extend(glob.glob("%s/conf/*" % app_folder))
    files_to_patch.extend(glob.glob("%s/scripts/*" % app_folder))
    files_to_patch.extend(glob.glob("%s/scripts/*/*" % app_folder))
    files_to_patch.extend(glob.glob("%s/scripts/.*" % app_folder))
    files_to_patch.append("%s/manifest.json" % app_folder)
    files_to_patch.append("%s/manifest.toml" % app_folder)

    for filename in files_to_patch:
        # Ignore non-regular files
        if not os.path.isfile(filename):
            continue

        c = (
            "sed -i "
            + "".join(f"-e 's@{p}@{r}@g' " for p, r in LEGACY_PHP_VERSION_REPLACEMENTS)
            + "%s" % filename
        )
        os.system(c)


def _patch_legacy_php_versions_in_settings(app_folder):
    settings = read_yaml(os.path.join(app_folder, "settings.yml"))

    if settings.get("fpm_config_dir") in ["/etc/php/7.0/fpm", "/etc/php/7.3/fpm", "/etc/php/7.4/fpm"]:
        settings["fpm_config_dir"] = "/etc/php/8.2/fpm"
    if settings.get("fpm_service") in ["php7.0-fpm", "php7.3-fpm", "php7.4-fpm"]:
        settings["fpm_service"] = "php8.2-fpm"
    if settings.get("phpversion") in ["7.0", "7.3", "7.4"]:
        settings["phpversion"] = "8.2"

    # We delete these checksums otherwise the file will appear as manually modified
    list_to_remove = [
        "checksum__etc_php_7.4_fpm_pool",
        "checksum__etc_php_7.3_fpm_pool",
        "checksum__etc_php_7.0_fpm_pool",
        "checksum__etc_nginx_conf.d",
    ]
    settings = {
        k: v
        for k, v in settings.items()
        if not any(k.startswith(to_remove) for to_remove in list_to_remove)
    }

    write_to_yaml(app_folder + "/settings.yml", settings)


def _patch_legacy_helpers(app_folder):
    files_to_patch = []
    files_to_patch.extend(glob.glob("%s/scripts/*" % app_folder))
    files_to_patch.extend(glob.glob("%s/scripts/.*" % app_folder))

    stuff_to_replace = {
        "yunohost user create": {
            "pattern": r"yunohost user create (\S+) (-f|--firstname) (\S+) (-l|--lastname) \S+ (.*)",
            "replace": r"yunohost user create \1 --fullname \3 \5",
            "important": False,
        },
        # Remove
        #    Automatic diagnosis data from YunoHost
        #    __PRE_TAG1__$(yunohost tools diagnosis | ...)__PRE_TAG2__"
        #
        "yunohost tools diagnosis": {
            "pattern": r"(Automatic diagnosis data from YunoHost( *\n)*)? *(__\w+__)? *\$\(yunohost tools diagnosis.*\)(__\w+__)?",
            "replace": r"",
            "important": False,
        },
    }

    for helper, infos in stuff_to_replace.items():
        infos["pattern"] = (
            re.compile(infos["pattern"]) if infos.get("pattern") else None
        )
        infos["replace"] = infos.get("replace")

    for filename in files_to_patch:
        # Ignore non-regular files
        if not os.path.isfile(filename):
            continue

        try:
            content = read_file(filename)
        except MoulinetteError:
            continue

        replaced_stuff = False
        show_warning = False

        for helper, infos in stuff_to_replace.items():
            # Ignore if not relevant for this file
            if infos.get("only_for") and not any(
                filename.endswith(f) for f in infos["only_for"]
            ):
                continue

            # If helper is used, attempt to patch the file
            if helper in content and infos["pattern"]:
                content = infos["pattern"].sub(infos["replace"], content)
                replaced_stuff = True
                if infos["important"]:
                    show_warning = True

            # If the helper is *still* in the content, it means that we
            # couldn't patch the deprecated helper in the previous lines.  In
            # that case, abort the install or whichever step is performed
            if helper in content and infos["important"]:
                raise YunohostValidationError(
                    "This app is likely pretty old and uses deprecated / outdated helpers that can't be migrated easily. It can't be installed anymore.",
                    raw_msg=True,
                )

        if replaced_stuff:
            # Check the app do load the helper
            # If it doesn't, add the instruction ourselve (making sure it's after the #!/bin/bash if it's there...
            if filename.split("/")[-1] in [
                "install",
                "remove",
                "upgrade",
                "backup",
                "restore",
            ]:
                source_helpers = "source /usr/share/yunohost/helpers"
                if source_helpers not in content:
                    content.replace("#!/bin/bash", "#!/bin/bash\n" + source_helpers)
                if source_helpers not in content:
                    content = source_helpers + "\n" + content

            # Actually write the new content in the file
            write_to_file(filename, content)

        if show_warning:
            # And complain about those damn deprecated helpers
            logger.error(
                r"/!\ Packagers! This app uses very old deprecated helpers... YunoHost automatically patched the helpers to use the new recommended practice, but please do consider fixing the upstream code right now..."
            )
