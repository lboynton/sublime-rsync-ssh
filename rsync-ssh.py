import sublime, sublime_plugin
import subprocess, os, re, threading, time

def console_print(prefix, folder, output):
    if prefix and folder:
        prefix = prefix + "[" + os.path.basename(folder) + "]: "
    elif prefix and not folder:
        prefix = prefix + ": "
    elif not prefix and folder:
        prefix = os.path.basename(folder) + ": "

    output = "[rsync-ssh] " + prefix + output.replace("\n", "\n[rsync-ssh] "+ prefix)
    print(output)


class RsyncSshInitSettingsCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        # Load project configuration
        project_data = sublime.active_window().project_data()

        # If no rsync-ssh config exists, then create it
        if not project_data.get('settings',{}).get("rsync_ssh"):
            if not project_data.get('settings'):
                project_data['settings'] = {}
            project_data['settings']["rsync_ssh"] = {}
            project_data['settings']["rsync_ssh"]["excludes"] = [
                '.git*', '_build', 'blib', 'Build'
            ]
            project_data['settings']["rsync_ssh"]["options"] = [
                "--dry-run",
                "--delete"
            ]

            project_data['settings']["rsync_ssh"]["remotes"] = {}
            for folder in project_data.get("folders"):
                project_data['settings']["rsync_ssh"]["remotes"][folder.get("path")] = [{
                    "remote_host": "my-server.my-domain.tld",
                    "remote_path": "/home/" + os.environ['USER'] + "/Projects/" + os.path.basename(folder.get("path")),
                    "remote_port": 22,
                    "remote_user": os.environ['USER'],
                    "enabled": 1,
                    "options": [],
                    "excludes": []
                }]

            # Save configuration
            sublime.active_window().set_project_data(project_data)

        # We won't clobber an existing configuration
        else:
            console_print("","","rsync_ssh configuration already exists.")

        # Open configuration in new tab
        sublime.active_window().run_command("open_file",  {"file": "${project}"})


class RsyncSshSaveCommand(sublime_plugin.EventListener):
    def on_post_save(self,view):
        # Get name of file being saved
        view.run_command("rsync_ssh_sync", {"file_being_saved": view.file_name()})


class RsyncSshSyncCommand(sublime_plugin.TextCommand):
    def run(self, edit, **args):
        # Convert list of project folders to dict indexed by path
        settings = sublime.active_window().active_view().settings().get("rsync_ssh")

        # Don't try to sync if User pressed ⌘⇧12 and rsync-ssh is unconfigured
        if not settings and not args.get("file_being_saved"):
            console_print("", "", "Aborting! - rsync ssh is not configured!")
            return
        # Don't try to sync when we have no settings
        elif not settings:
            return

        # Start command thread to keep ui responsive
        thread = RsyncSSH(settings, args.get("file_being_saved", ""))
        thread.start()


class RsyncSSH(threading.Thread):
    def __init__(self, settings, file_being_saved):
        self.settings         = settings
        self.file_being_saved = file_being_saved
        threading.Thread.__init__(self)

    def run(self):
        # Don't sync git commit message buffer
        if self.file_being_saved and os.path.basename(self.file_being_saved) == "COMMIT_EDITMSG":
            return

        # Merge settings with defaults
        global_excludes = [ ".DS_Store" ]
        global_excludes.extend( self.settings.get("excludes", []) )

        global_options = []
        global_options.extend( self.settings.get("options", []) )

        connect_timeout = self.settings.get("timeout", 5)

        # Iterate over all active folders and start a sync thread for each one
        for full_folder_path in sublime.active_window().folders():
            basename = os.path.basename(full_folder_path)
            remotes = self.settings.get("remotes").get(basename)

            # Don't sync if saving single file outside of project file
            if self.file_being_saved and not self.file_being_saved.startswith(full_folder_path+"/"):
                continue

            # Don't sync if no remotes are defined
            if remotes == None:
                console_print("", basename, "No remotes defined for "+basename)
                continue

            threads = []
            for remote in remotes:
                local_excludes = list(global_excludes)
                local_excludes.extend(remote.get("excludes", []))

                local_options = list(global_options)
                local_options.extend(remote.get("options", []))

                thread = Rsync(
                    full_folder_path,
                    remote,
                    local_excludes,
                    local_options,
                    connect_timeout,
                    full_folder_path,
                    self.file_being_saved
                )
                threads.append(thread)
                thread.start()


class Rsync(threading.Thread):
    def __init__(self, local_path, remote, excludes, options, timeout, project_path, single_file):
        self.local_path   = local_path
        self.remote       = remote
        self.excludes     = excludes
        self.options      = options
        self.timeout      = timeout
        self.project_path = project_path
        self.single_file  = single_file
        threading.Thread.__init__(self)

    def run(self):
        # Skip disabled remotes
        if not self.remote.get("enabled", 1):
            console_print(self.remote.get("remote_host"), self.local_path, "Skipping, host is disabled.")
            return

        # What to rsync
        source_path      = self.local_path + "/"
        destination_path = self.remote.get("remote_path")

        # Handle single file syncs (save events)
        if self.single_file and self.single_file.startswith(self.project_path+"/"):
            source_path = self.single_file
            destination_path = self.remote.get("remote_path") + self.single_file.replace(self.project_path, "")

        # Check ssh connection, and verify that rsync exists in path on the remote host
        check_command = [
            "ssh", "-p", str(self.remote.get("remote_port", "22")),
            self.remote.get("remote_user")+"@"+self.remote.get("remote_host"),
            "type rsync"
        ]
        try:
            output = subprocess.check_output(check_command, universal_newlines=True, timeout=self.timeout, stderr=subprocess.STDOUT)
            if not re.match("rsync is", output):
                message = "ERROR: Unable to locate rsync on "+self.remote.get("remote_host")
                console_print(self.remote.get("remote_host"), self.local_path, message)
                sublime.active_window().run_command("terminal_notifier", {
                    "title": "\[Rsync SSH] - ERROR",
                    "subtitle": self.remote.get("remote_host"),
                    "message": message,
                    "group": self.remote.get("remote_host")+":"+self.remote.get("remote_path")
                })
                console_print(self.remote.get("remote_host"), self.local_path, output)
                return
        except subprocess.TimeoutExpired as e:
            console_print(self.remote.get("remote_host"), self.local_path, "ERROR: "+e.output)
            sublime.active_window().run_command("terminal_notifier", {
                "title": "\[Rsync SSH] - ERROR Timeout",
                "subtitle": self.remote.get("remote_host"),
                "message": e.output
            })
            return
        except subprocess.CalledProcessError as e:
            console_print(self.remote.get("remote_host"), self.local_path, "ERROR: "+e.output)
            sublime.active_window().run_command("terminal_notifier", {
                "title": "\[Rsync SSH] - ERROR command failed",
                "subtitle": self.remote.get("remote_host"),
                "message": e.output
            })
            return

        # Build rsync command
        rsync_command = [
            "rsync", "-v", "-zar",
            "-e", "ssh -p " + str(self.remote.get("remote_port", "22")) + " -o ConnectTimeout="+str(self.timeout)
        ]
        rsync_command.extend(self.options)
        rsync_command.extend([
            source_path,
            self.remote.get("remote_user")+"@"+self.remote.get("remote_host")+":"+destination_path
        ])
        console_print(self.remote.get("remote_host"), self.local_path, " ".join(rsync_command))

        # Add excludes
        for exclude in set(self.excludes):
            rsync_command.append("--exclude="+exclude)

        # Execute rsync
        try:
            output = subprocess.check_output(rsync_command, universal_newlines=True, stderr=subprocess.STDOUT)
            console_print(self.remote.get("remote_host"), self.local_path, output)
        except subprocess.CalledProcessError as e:
            console_print(self.remote.get("remote_host"), self.local_path, "ERROR: "+e.output+"\n")
            sublime.active_window().run_command("terminal_notifier", {
                "title": "\[Rsync SSH] - ERROR",
                "subtitle": self.remote.get("remote_host"),
                "message": "rsync failed."
            })

        sublime.active_window().run_command("terminal_notifier", {
            "title": "\[Rsync SSH] - OK",
            "subtitle": self.remote.get("remote_host")+"["+os.path.basename(self.local_path)+"]",
            "message": "rsync of '" + os.path.basename(self.local_path) + "' complete.",
            "group": self.remote.get("remote_host")+"_"+self.remote.get("remote_path")
        })

        return
