# coding=utf-8


import octoprint.plugin

class TestDeprecatedAssetPlugin(octoprint.plugin.AssetPlugin):
	pass


class TestSecondaryDeprecatedAssetPlugin(octoprint.plugin.AssetPlugin):
	pass


__plugin_name__ = "Deprecated Plugin"
__plugin_description__ = "Test deprecated plugin"
__plugin_implementations__ = [TestDeprecatedAssetPlugin(), TestSecondaryDeprecatedAssetPlugin()]