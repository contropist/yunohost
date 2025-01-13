ynhtest_settings() {

    test -n "$app"

    mkdir -p "/etc/yunohost/apps/$app"
    echo "label: $app" > "/etc/yunohost/apps/$app/settings.yml"

    test -z "$(ynh_app_setting_get --key="foo")"
    test -z "$(ynh_app_setting_get --key="bar")"
    test -z "$(ynh_app_setting_get --app="$app" --key="baz")"

    ynh_app_setting_set --key="foo" --value="foovalue"
    ynh_app_setting_set --app="$app" --key="bar" --value="barvalue"
    ynh_app_setting_set "$app" baz bazvalue
    
    test "$(ynh_app_setting_get --key="foo")" == "foovalue"
    test "$(ynh_app_setting_get --key="bar")" == "barvalue"
    test "$(ynh_app_setting_get --app="$app" --key="baz")" == "bazvalue"
    
    ynh_app_setting_delete --key="foo"
    ynh_app_setting_delete --app="$app" --key="bar"
    ynh_app_setting_delete "$app" baz

    test -z "$(ynh_app_setting_get --key="foo")"
    test -z "$(ynh_app_setting_get --key="bar")"
    test -z "$(ynh_app_setting_get --app="$app" --key="baz")"

    rm -rf "/etc/yunohost/apps/$app"
}
