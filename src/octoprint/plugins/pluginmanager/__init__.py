# coding=utf-8


__author__ = "Gina Häußge <osd@foosel.net>"
__license__ = 'GNU Affero General Public License http://www.gnu.org/licenses/agpl.html'
__copyright__ = "Copyright (C) 2015 The OctoPrint Project - Released under terms of the AGPLv3 License"


import octoprint.plugin
import octoprint.plugin.core

from octoprint.settings import valid_boolean_trues
from octoprint.server.util.flask import restricted_access, with_revalidation_checking, check_etag
from octoprint.server import admin_permission, VERSION
from octoprint.util.pip import LocalPipCaller, UnknownPip

from flask import jsonify, make_response
from flask.ext.babel import gettext

import logging
import sarge
import sys
import requests
import re
import os
import pkg_resources

class PluginManagerPlugin(octoprint.plugin.SimpleApiPlugin,
                          octoprint.plugin.TemplatePlugin,
                          octoprint.plugin.AssetPlugin,
                          octoprint.plugin.SettingsPlugin,
                          octoprint.plugin.StartupPlugin,
                          octoprint.plugin.BlueprintPlugin):

	ARCHIVE_EXTENSIONS = (".zip", ".tar.gz", ".tgz", ".tar")

	OPERATING_SYSTEMS = dict(windows=["win32"],
	                         linux=["linux2"],
	                         macos=["darwin"])

	pip_inapplicable_arguments = dict(uninstall=["--user"])

	def __init__(self):
		self._pending_enable = set()
		self._pending_disable = set()
		self._pending_install = set()
		self._pending_uninstall = set()

		self._pip_caller = None

		self._repository_available = False
		self._repository_plugins = []
		self._repository_cache_path = None
		self._repository_cache_ttl = 0

		self._console_logger = None

	def initialize(self):
		self._console_logger = logging.getLogger("octoprint.plugins.pluginmanager.console")
		self._repository_cache_path = os.path.join(self.get_plugin_data_folder(), "plugins.json")
		self._repository_cache_ttl = self._settings.get_int(["repository_ttl"]) * 60

		self._pip_caller = LocalPipCaller(force_user=self._settings.get_boolean(["pip_force_user"]))
		self._pip_caller.on_log_call = self._log_call
		self._pip_caller.on_log_stdout = self._log_stdout
		self._pip_caller.on_log_stderr = self._log_stderr

	##~~ Body size hook

	def increase_upload_bodysize(self, current_max_body_sizes, *args, **kwargs):
		# set a maximum body size of 50 MB for plugin archive uploads
		return [("POST", r"/upload_archive", 50 * 1024 * 1024)]

	##~~ StartupPlugin

	def on_startup(self, host, port):
		from octoprint.logging.handlers import CleaningTimedRotatingFileHandler
		console_logging_handler = CleaningTimedRotatingFileHandler(self._settings.get_plugin_logfile_path(postfix="console"), when="D", backupCount=3)
		console_logging_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
		console_logging_handler.setLevel(logging.DEBUG)

		self._console_logger.addHandler(console_logging_handler)
		self._console_logger.setLevel(logging.DEBUG)
		self._console_logger.propagate = False

		self._repository_available = self._fetch_repository_from_disk()

	##~~ SettingsPlugin

	def get_settings_defaults(self):
		return dict(
			repository="http://plugins.octoprint.org/plugins.json",
			repository_ttl=24*60,
			pip_args=None,
			pip_force_user=False,
			dependency_links=False,
			hidden=[]
		)

	def on_settings_save(self, data):
		octoprint.plugin.SettingsPlugin.on_settings_save(self, data)

		self._repository_cache_ttl = self._settings.get_int(["repository_ttl"]) * 60
		self._pip_caller.force_user = self._settings.get_boolean(["pip_force_user"])

	##~~ AssetPlugin

	def get_assets(self):
		return dict(
			js=["js/pluginmanager.js"],
			css=["css/pluginmanager.css"],
			less=["less/pluginmanager.less"]
		)

	##~~ TemplatePlugin

	def get_template_configs(self):
		return [
			dict(type="settings", name=gettext("Plugin Manager"), template="pluginmanager_settings.jinja2", custom_bindings=True),
			dict(type="about", name="Plugin Licenses", template="pluginmanager_about.jinja2")
		]

	def get_template_vars(self):
		plugins = sorted(self._get_plugins(), key=lambda x: x["name"].lower())
		return dict(
			all=plugins,
			thirdparty=[p for p in plugins if not p["bundled"]],
			archive_extensions=self.__class__.ARCHIVE_EXTENSIONS
		)

	def get_template_types(self, template_sorting, template_rules, *args, **kwargs):
		return [
			("about_thirdparty", dict(), dict(template=lambda x: x + "_about_thirdparty.jinja2"))
		]

	##~~ BlueprintPlugin

	@octoprint.plugin.BlueprintPlugin.route("/upload_archive", methods=["POST"])
	@restricted_access
	@admin_permission.require(403)
	def upload_archive(self):
		import flask

		input_name = "file"
		input_upload_path = input_name + "." + self._settings.global_get(["server", "uploads", "pathSuffix"])
		input_upload_name = input_name + "." + self._settings.global_get(["server", "uploads", "nameSuffix"])

		if input_upload_path not in flask.request.values or input_upload_name not in flask.request.values:
			return flask.make_response("No file included", 400)
		upload_path = flask.request.values[input_upload_path]
		upload_name = flask.request.values[input_upload_name]

		exts = [x for x in self.__class__.ARCHIVE_EXTENSIONS if upload_name.lower().endswith(x)]
		if not len(exts):
			return flask.make_response("File doesn't have a valid extension for a plugin archive", 400)

		ext = exts[0]

		import tempfile
		import shutil
		import os

		archive = tempfile.NamedTemporaryFile(delete=False, suffix="{ext}".format(**locals()))
		try:
			archive.close()
			shutil.copy(upload_path, archive.name)
			return self.command_install(path=archive.name, force="force" in flask.request.values and flask.request.values["force"] in valid_boolean_trues)
		finally:
			try:
				os.remove(archive.name)
			except Exception as e:
				self._logger.warn("Could not remove temporary file {path} again: {message}".format(path=archive.name, message=str(e)))

	##~~ SimpleApiPlugin

	def get_api_commands(self):
		return {
			"install": ["url"],
			"uninstall": ["plugin"],
			"enable": ["plugin"],
			"disable": ["plugin"],
			"refresh_repository": []
		}

	def on_api_get(self, request):
		if not admin_permission.can():
			return make_response("Insufficient rights", 403)

		from octoprint.server import safe_mode

		refresh_repository = request.values.get("refresh_repository", "false") in valid_boolean_trues
		if refresh_repository:
			self._repository_available = self._refresh_repository()

		def view():
			return jsonify(plugins=self._get_plugins(),
			               repository=dict(
			                   available=self._repository_available,
			                   plugins=self._repository_plugins
			               ),
			               os=self._get_os(),
			               octoprint=self._get_octoprint_version_string(),
			               pip=dict(
			                   available=self._pip_caller.available,
			                   version=self._pip_caller.version_string,
			                   install_dir=self._pip_caller.install_dir,
			                   use_user=self._pip_caller.use_user,
			                   virtual_env=self._pip_caller.virtual_env,
			                   additional_args=self._settings.get(["pip_args"]),
			                   python=sys.executable
		                    ),
			               safe_mode=safe_mode)

		def etag():
			import hashlib
			hash = hashlib.sha1()
			hash.update(repr(self._get_plugins()))
			hash.update(str(self._repository_available))
			hash.update(repr(self._repository_plugins))
			hash.update(repr(safe_mode))
			return hash.hexdigest()

		def condition():
			return check_etag(etag())

		return with_revalidation_checking(etag_factory=lambda *args, **kwargs: etag(),
		                                  condition=lambda *args, **kwargs: condition(),
		                                  unless=lambda: refresh_repository)(view)()

	def on_api_command(self, command, data):
		if not admin_permission.can():
			return make_response("Insufficient rights", 403)

		if self._printer.is_printing() or self._printer.is_paused():
			# do not update while a print job is running
			return make_response("Printer is currently printing or paused", 409)

		if command == "install":
			url = data["url"]
			plugin_name = data["plugin"] if "plugin" in data else None
			return self.command_install(url=url,
			                            force="force" in data and data["force"] in valid_boolean_trues,
			                            dependency_links="dependency_links" in data and data["dependency_links"] in valid_boolean_trues,
			                            reinstall=plugin_name)

		elif command == "uninstall":
			plugin_name = data["plugin"]
			if not plugin_name in self._plugin_manager.plugins:
				return make_response("Unknown plugin: %s" % plugin_name, 404)

			plugin = self._plugin_manager.plugins[plugin_name]
			return self.command_uninstall(plugin)

		elif command == "enable" or command == "disable":
			plugin_name = data["plugin"]
			if not plugin_name in self._plugin_manager.plugins:
				return make_response("Unknown plugin: %s" % plugin_name, 404)

			plugin = self._plugin_manager.plugins[plugin_name]
			return self.command_toggle(plugin, command)

	def command_install(self, url=None, path=None, force=False, reinstall=None, dependency_links=False):
		if url is not None:
			pip_args = ["install", sarge.shell_quote(url)]
		elif path is not None:
			pip_args = ["install", sarge.shell_quote(path)]
		else:
			raise ValueError("Either URL or path must be provided")

		if dependency_links or self._settings.get_boolean(["dependency_links"]):
			pip_args.append("--process-dependency-links")

		all_plugins_before = self._plugin_manager.find_plugins()

		success_string = "Successfully installed"
		failure_string = "Could not install"
		try:
			returncode, stdout, stderr = self._call_pip(pip_args)
		except:
			self._logger.exception("Could not install plugin from %s" % url)
			return make_response("Could not install plugin from URL, see the log for more details", 500)
		else:
			if force:
				pip_args += ["--ignore-installed", "--force-reinstall", "--no-deps"]
				try:
					returncode, stdout, stderr = self._call_pip(pip_args)
				except:
					self._logger.exception("Could not install plugin from %s" % url)
					return make_response("Could not install plugin from URL, see the log for more details", 500)

		try:
			result_line = filter(lambda x: x.startswith(success_string) or x.startswith(failure_string), stdout)[-1]
		except IndexError:
			result = dict(result=False, reason="Could not parse output from pip")
			self._send_result_notification("install", result)
			return jsonify(result)

		# The final output of a pip install command looks something like this:
		#
		#   Successfully installed OctoPrint-Plugin-1.0 Dependency-One-0.1 Dependency-Two-9.3
		#
		# or this:
		#
		#   Successfully installed OctoPrint-Plugin Dependency-One Dependency-Two
		#   Cleaning up...
		#
		# So we'll need to fetch the "Successfully installed" line, strip the "Successfully" part, then split by whitespace
		# and strip to get all installed packages.
		#
		# We then need to iterate over all known plugins and see if either the package name or the package name plus
		# version number matches one of our installed packages. If it does, that's our installed plugin.
		#
		# Known issue: This might return the wrong plugin if more than one plugin was installed through this
		# command (e.g. due to pulling in another plugin as dependency). It should be safe for now though to
		# consider this a rare corner case. Once it becomes a real problem we'll just extend the plugin manager
		# so that it can report on more than one installed plugin.

		result_line = result_line.strip()
		if not result_line.startswith(success_string):
			result = dict(result=False, reason="Pip did not report successful installation")
			self._send_result_notification("install", result)
			return jsonify(result)

		installed = [x.strip() for x in result_line[len(success_string):].split(" ")]
		all_plugins_after = self._plugin_manager.find_plugins(existing=dict(), ignore_uninstalled=False)

		for key, plugin in list(all_plugins_after.items()):
			if plugin.origin is None or plugin.origin.type != "entry_point":
				continue

			package_name = plugin.origin.package_name
			package_version = plugin.origin.package_version
			versioned_package = "{package_name}-{package_version}".format(**locals())

			if package_name in installed or versioned_package in installed:
				# exact match, we are done here
				new_plugin_key = key
				new_plugin = plugin
				break

			else:
				# it might still be a version that got stripped by python's package resources, e.g. 1.4.5a0 => 1.4.5a
				found = False

				for inst in installed:
					if inst.startswith(versioned_package):
						found = True
						break

				if found:
					new_plugin_key = key
					new_plugin = plugin
					break
		else:
			self._logger.warn("The plugin was installed successfully, but couldn't be found afterwards to initialize properly during runtime. Please restart OctoPrint.")
			result = dict(result=True, url=url, needs_restart=True, needs_refresh=True, was_reinstalled=False, plugin="unknown")
			self._send_result_notification("install", result)
			return jsonify(result)

		self._plugin_manager.reload_plugins()
		needs_restart = self._plugin_manager.is_restart_needing_plugin(new_plugin) or new_plugin_key in all_plugins_before or reinstall is not None
		needs_refresh = new_plugin.implementation and isinstance(new_plugin.implementation, octoprint.plugin.ReloadNeedingPlugin)

		is_reinstall = self._plugin_manager.is_plugin_marked(new_plugin_key, "uninstalled")
		self._plugin_manager.mark_plugin(new_plugin_key,
		                                 uninstalled=False,
		                                 installed=not is_reinstall and needs_restart)

		self._plugin_manager.log_all_plugins()

		result = dict(result=True, url=url, needs_restart=needs_restart, needs_refresh=needs_refresh, was_reinstalled=new_plugin_key in all_plugins_before or reinstall is not None, plugin=self._to_external_representation(new_plugin))
		self._send_result_notification("install", result)
		return jsonify(result)

	def command_uninstall(self, plugin):
		if plugin.key == "pluginmanager":
			return make_response("Can't uninstall Plugin Manager", 403)

		if not plugin.managable:
			return make_response("Plugin is not managable and hence cannot be uninstalled", 403)

		if plugin.bundled:
			return make_response("Bundled plugins cannot be uninstalled", 403)

		if plugin.origin is None:
			self._logger.warn("Trying to uninstall plugin {plugin} but origin is unknown".format(**locals()))
			return make_response("Could not uninstall plugin, its origin is unknown")

		if plugin.origin.type == "entry_point":
			# plugin is installed through entry point, need to use pip to uninstall it
			origin = plugin.origin[3]
			if origin is None:
				origin = plugin.origin[2]

			pip_args = ["uninstall", "--yes", origin]
			try:
				self._call_pip(pip_args)
			except:
				self._logger.exception("Could not uninstall plugin via pip")
				return make_response("Could not uninstall plugin via pip, see the log for more details", 500)

		elif plugin.origin.type == "folder":
			import os
			import shutil
			full_path = os.path.realpath(plugin.location)

			if os.path.isdir(full_path):
				# plugin is installed via a plugin folder, need to use rmtree to get rid of it
				self._log_stdout("Deleting plugin from {folder}".format(folder=plugin.location))
				shutil.rmtree(full_path)
			elif os.path.isfile(full_path):
				self._log_stdout("Deleting plugin from {file}".format(file=plugin.location))
				os.remove(full_path)

				if full_path.endswith(".py"):
					pyc_file = "{full_path}c".format(**locals())
					if os.path.isfile(pyc_file):
						os.remove(pyc_file)

		else:
			self._logger.warn("Trying to uninstall plugin {plugin} but origin is unknown ({plugin.origin.type})".format(**locals()))
			return make_response("Could not uninstall plugin, its origin is unknown")

		needs_restart = self._plugin_manager.is_restart_needing_plugin(plugin)
		needs_refresh = plugin.implementation and isinstance(plugin.implementation, octoprint.plugin.ReloadNeedingPlugin)

		was_pending_install = self._plugin_manager.is_plugin_marked(plugin.key, "installed")
		self._plugin_manager.mark_plugin(plugin.key,
		                                 uninstalled=not was_pending_install and needs_restart,
		                                 installed=False)

		if not needs_restart:
			try:
				self._plugin_manager.disable_plugin(plugin.key, plugin=plugin)
			except octoprint.plugin.core.PluginLifecycleException as e:
				self._logger.exception("Problem disabling plugin {name}".format(name=plugin.key))
				result = dict(result=False, uninstalled=True, disabled=False, unloaded=False, reason=e.reason)
				self._send_result_notification("uninstall", result)
				return jsonify(result)

			try:
				self._plugin_manager.unload_plugin(plugin.key)
			except octoprint.plugin.core.PluginLifecycleException as e:
				self._logger.exception("Problem unloading plugin {name}".format(name=plugin.key))
				result = dict(result=False, uninstalled=True, disabled=True, unloaded=False, reason=e.reason)
				self._send_result_notification("uninstall", result)
				return jsonify(result)

		self._plugin_manager.reload_plugins()

		result = dict(result=True, needs_restart=needs_restart, needs_refresh=needs_refresh, plugin=self._to_external_representation(plugin))
		self._send_result_notification("uninstall", result)
		return jsonify(result)

	def command_toggle(self, plugin, command):
		if plugin.key == "pluginmanager":
			return make_response("Can't enable/disable Plugin Manager", 400)

		needs_restart = self._plugin_manager.is_restart_needing_plugin(plugin)
		needs_refresh = plugin.implementation and isinstance(plugin.implementation, octoprint.plugin.ReloadNeedingPlugin)

		pending = ((command == "disable" and plugin.key in self._pending_enable) or (command == "enable" and plugin.key in self._pending_disable))
		safe_mode_victim = getattr(plugin, "safe_mode_victim", False)
		needs_restart_api = (needs_restart or safe_mode_victim) and not pending
		needs_refresh_api = needs_refresh and not pending

		try:
			if command == "disable":
				self._mark_plugin_disabled(plugin, needs_restart=needs_restart)
			elif command == "enable":
				self._mark_plugin_enabled(plugin, needs_restart=needs_restart)
		except octoprint.plugin.core.PluginLifecycleException as e:
			self._logger.exception("Problem toggling enabled state of {name}: {reason}".format(name=plugin.key, reason=e.reason))
			result = dict(result=False, reason=e.reason)
		except octoprint.plugin.core.PluginNeedsRestart:
			result = dict(result=True, needs_restart=True, needs_refresh=True, plugin=self._to_external_representation(plugin))
		else:
			result = dict(result=True, needs_restart=needs_restart_api, needs_refresh=needs_refresh_api, plugin=self._to_external_representation(plugin))

		self._send_result_notification(command, result)
		return jsonify(result)

	def _send_result_notification(self, action, result):
		notification = dict(type="result", action=action)
		notification.update(result)
		self._plugin_manager.send_plugin_message(self._identifier, notification)

	def _call_pip(self, args):
		if self._pip_caller is None or not self._pip_caller.available:
			raise RuntimeError("No pip available, can't operate".format(**locals()))

		if "--process-dependency-links" in args:
			self._log_message("Installation needs to process external dependencies, that might make it take a bit longer than usual depending on the pip version")

		additional_args = self._settings.get(["pip_args"])

		if additional_args is not None:

			inapplicable_arguments = self.__class__.pip_inapplicable_arguments.get(args[0], list())
			for inapplicable_argument in inapplicable_arguments:
				additional_args = re.sub("(^|\s)" + re.escape(inapplicable_argument) + "\\b", "", additional_args)

			if additional_args:
				args.append(additional_args)

		return self._pip_caller.execute(*args)

	def _log_message(self, *lines):
		self._log(lines, prefix="*", stream="message")

	def _log_call(self, *lines):
		self._log(lines, prefix=" ", stream="call")

	def _log_stdout(self, *lines):
		self._log(lines, prefix=">", stream="stdout")

	def _log_stderr(self, *lines):
		self._log(lines, prefix="!", stream="stderr")

	def _log(self, lines, prefix=None, stream=None, strip=True):
		if strip:
			lines = [x.strip() for x in lines]

		self._plugin_manager.send_plugin_message(self._identifier, dict(type="loglines", loglines=[dict(line=line, stream=stream) for line in lines]))
		for line in lines:
			self._console_logger.debug("{prefix} {line}".format(**locals()))

	def _mark_plugin_enabled(self, plugin, needs_restart=False):
		disabled_list = list(self._settings.global_get(["plugins", "_disabled"]))
		if plugin.key in disabled_list:
			disabled_list.remove(plugin.key)
			self._settings.global_set(["plugins", "_disabled"], disabled_list)
			self._settings.save(force=True)

		if not needs_restart and not getattr(plugin, "safe_mode_victim", False):
			self._plugin_manager.enable_plugin(plugin.key)
		else:
			if plugin.key in self._pending_disable:
				self._pending_disable.remove(plugin.key)
			elif (not plugin.enabled and not getattr(plugin, "safe_mode_enabled", False)) and plugin.key not in self._pending_enable:
				self._pending_enable.add(plugin.key)

	def _mark_plugin_disabled(self, plugin, needs_restart=False):
		disabled_list = list(self._settings.global_get(["plugins", "_disabled"]))
		if not plugin.key in disabled_list:
			disabled_list.append(plugin.key)
			self._settings.global_set(["plugins", "_disabled"], disabled_list)
			self._settings.save(force=True)

		if not needs_restart and not getattr(plugin, "safe_mode_victim", False):
			self._plugin_manager.disable_plugin(plugin.key)
		else:
			if plugin.key in self._pending_enable:
				self._pending_enable.remove(plugin.key)
			elif (plugin.enabled or getattr(plugin, "safe_mode_enabled", False)) and plugin.key not in self._pending_disable:
				self._pending_disable.add(plugin.key)

	def _fetch_repository_from_disk(self):
		repo_data = None
		if os.path.isfile(self._repository_cache_path):
			import time
			mtime = os.path.getmtime(self._repository_cache_path)
			if mtime + self._repository_cache_ttl >= time.time() > mtime:
				try:
					import json
					with open(self._repository_cache_path) as f:
						repo_data = json.load(f)
					self._logger.info("Loaded plugin repository data from disk, was still valid")
				except:
					self._logger.exception("Error while loading repository data from {}".format(self._repository_cache_path))

		return self._refresh_repository(repo_data=repo_data)

	def _fetch_repository_from_url(self):
		import requests
		repository_url = self._settings.get(["repository"])
		try:
			r = requests.get(repository_url)
			self._logger.info("Loaded plugin repository data from {}".format(repository_url))
		except Exception as e:
			self._logger.exception("Could not fetch plugins from repository at {repository_url}: {message}".format(repository_url=repository_url, message=str(e)))
			return None

		repo_data = r.json()

		try:
			import json
			with octoprint.util.atomic_write(self._repository_cache_path, "wb") as f:
				json.dump(repo_data, f)
		except Exception as e:
			self._logger.exception("Error while saving repository data to {}: {}".format(self._repository_cache_path, str(e)))

		return repo_data

	def _refresh_repository(self, repo_data=None):
		if repo_data is None:
			repo_data = self._fetch_repository_from_url()
			if repo_data is None:
				return False

		current_os = self._get_os()
		octoprint_version = self._get_octoprint_version(base=True)

		def map_repository_entry(entry):
			result = dict(entry)

			if not "follow_dependency_links" in result:
				result["follow_dependency_links"] = False

			result["is_compatible"] = dict(
				octoprint=True,
				os=True
			)

			if "compatibility" in entry:
				if "octoprint" in entry["compatibility"] and entry["compatibility"]["octoprint"] is not None and isinstance(entry["compatibility"]["octoprint"], (list, tuple)) and len(entry["compatibility"]["octoprint"]):
					result["is_compatible"]["octoprint"] = self._is_octoprint_compatible(octoprint_version, entry["compatibility"]["octoprint"])

				if "os" in entry["compatibility"] and entry["compatibility"]["os"] is not None and isinstance(entry["compatibility"]["os"], (list, tuple)) and len(entry["compatibility"]["os"]):
					result["is_compatible"]["os"] = self._is_os_compatible(current_os, entry["compatibility"]["os"])

			return result

		self._repository_plugins = list(map(map_repository_entry, repo_data))
		return True

	def _is_octoprint_compatible(self, octoprint_version, compatibility_entries):
		"""
		Tests if the current ``octoprint_version`` is compatible to any of the provided ``compatibility_entries``.
		"""

		for octo_compat in compatibility_entries:
			try:
				if not any(octo_compat.startswith(c) for c in ("<", "<=", "!=", "==", ">=", ">", "~=", "===")):
					octo_compat = ">={}".format(octo_compat)

				s = next(pkg_resources.parse_requirements("OctoPrint" + octo_compat))
				if octoprint_version in s:
					break
			except:
				self._logger.exception("Something is wrong with this compatibility string for OctoPrint: {}".format(octo_compat))
		else:
			return False

		return True

	def _is_os_compatible(self, current_os, compatibility_entries):
		"""
		Tests if the ``current_os`` matches any of the provided ``compatibility_entries``.
		"""
		return current_os in [x for x in compatibility_entries if x in list(self.__class__.OPERATING_SYSTEMS.keys())]

	def _get_os(self):
		for identifier, platforms in list(self.__class__.OPERATING_SYSTEMS.items()):
			if sys.platform in platforms:
				return identifier
		else:
			return "unknown"

	def _get_octoprint_version_string(self):
		return VERSION

	def _get_octoprint_version(self, base=False):
		octoprint_version_string = self._get_octoprint_version_string()

		if "-" in octoprint_version_string:
			octoprint_version_string = octoprint_version_string[:octoprint_version_string.find("-")]

		octoprint_version = pkg_resources.parse_version(octoprint_version_string)

		# A leading v is common in github release tags and old setuptools doesn't remove it. While OctoPrint's
		# versions should never contains such a prefix, we'll make sure to have stuff behave the same
		# regardless of setuptools version anyhow.
		if octoprint_version and isinstance(octoprint_version, tuple) and octoprint_version[0].lower() == "*v":
			octoprint_version = octoprint_version[1:]

		if base:
			if isinstance(octoprint_version, tuple):
				# old setuptools
				base_version = []
				for part in octoprint_version:
					if part.startswith("*"):
						break
					base_version.append(part)
				base_version.append("*final")
				octoprint_version = tuple(base_version)
			else:
				# new setuptools
				octoprint_version = pkg_resources.parse_version(octoprint_version.base_version)
		return octoprint_version

	def _get_plugins(self):
		plugins = self._plugin_manager.plugins

		hidden = self._settings.get(["hidden"])
		result = []
		for name, plugin in list(plugins.items()):
			if name in hidden:
				continue
			result.append(self._to_external_representation(plugin))

		return result

	def _to_external_representation(self, plugin):
		return dict(
			key=plugin.key,
			name=plugin.name,
			description=plugin.description,
			author=plugin.author,
			version=plugin.version,
			url=plugin.url,
			license=plugin.license,
			bundled=plugin.bundled,
			managable=plugin.managable,
			enabled=plugin.enabled,
			safe_mode_victim=getattr(plugin, "safe_mode_victim", False),
			safe_mode_enabled=getattr(plugin, "safe_mode_enabled", False),
			pending_enable=(not plugin.enabled and not getattr(plugin, "safe_mode_enabled", False) and plugin.key in self._pending_enable),
			pending_disable=((plugin.enabled or getattr(plugin, "safe_mode_enabled", False)) and plugin.key in self._pending_disable),
			pending_install=(self._plugin_manager.is_plugin_marked(plugin.key, "installed")),
			pending_uninstall=(self._plugin_manager.is_plugin_marked(plugin.key, "uninstalled")),
			origin=plugin.origin.type
		)

__plugin_name__ = "Plugin Manager"
__plugin_author__ = "Gina Häußge"
__plugin_url__ = "https://github.com/foosel/OctoPrint/wiki/Plugin:-Plugin-Manager"
__plugin_description__ = "Allows installing and managing OctoPrint plugins"
__plugin_license__ = "AGPLv3"

def __plugin_load__():
	global __plugin_implementation__
	__plugin_implementation__ = PluginManagerPlugin()

	global __plugin_hooks__
	__plugin_hooks__ = {
		"octoprint.server.http.bodysize": __plugin_implementation__.increase_upload_bodysize,
		"octoprint.ui.web.templatetypes": __plugin_implementation__.get_template_types
	}
