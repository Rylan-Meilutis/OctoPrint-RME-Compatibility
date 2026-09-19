"""OctoPod's OctoLight HA API, backed exclusively by RME chamber lighting.

Do not enable alongside a real octolightHA plugin (the identifier is shared).
No GPIO access, Home Assistant connection or persistent lighting state.
"""
import importlib.util
import flask
import octoprint.plugin
from octoprint.access.permissions import Permissions

__plugin_name__ = "RME OctoPod Lighting Bridge"
__plugin_pythoncompat__ = ">=3.8,<4"


def __plugin_check__():
    # Never take over an installed real Home Assistant light integration.
    return importlib.util.find_spec("octoprint_octolightHA") is None


class RmeOctopodLight(octoprint.plugin.SimpleApiPlugin,
                      octoprint.plugin.SettingsPlugin):
    def get_settings_defaults(self):
        # OctoPod discovers plugins through the settings plugins dictionary.
        return {"backend": "rme_compatibility"}

    def on_api_get(self, request):
        action = request.args.get("action", "getState")
        permission = Permissions.STATUS if action == "getState" else Permissions.CONTROL
        if not permission.can():
            return flask.abort(403)
        if action not in ("getState", "toggle", "turnOn", "turnOff"):
            return flask.abort(400)
        info = self._plugin_manager.get_plugin_info("rme_compatibility", require_enabled=True)
        backend = info.implementation if info else None
        if backend is None:
            return flask.abort(503)
        with backend._state_lock:
            state = backend._state
            mode = state.get("tune", {}).get("light", -1)
            available = state.get("connected") and state.get("machine", {}).get("tune") == 1
        if not available or mode < 0:
            return flask.abort(503)
        on = mode > 0
        if action != "getState":
            on = not on if action == "toggle" else action == "turnOn"
            try:
                backend._set_print_override({"kind": "light", "value": int(on)})
            except (ValueError, RuntimeError):
                return flask.abort(409)
        # Mutation returns requested state; getState always returns telemetry.
        return flask.jsonify(state=on, pending=action != "getState")


__plugin_implementation__ = RmeOctopodLight()
