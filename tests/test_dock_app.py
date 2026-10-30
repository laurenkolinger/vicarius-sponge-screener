"""Tests for tools/install_dock_app.sh and the launcher it writes.

macOS stops a process that a foreground app leaves behind when the app exits,
so the Dock app has launchd run the server. These tests run the real installer
and the real launcher in a temporary home folder, with every command that would
touch the Mac replaced by a stub that records its arguments.
"""

import os
import plistlib
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LABEL = "edu.uvi.vicar.sponge-screener.server"
# A folder name with every character that broke or could break the generated files.
AWKWARD_NAME = "Reef & Video's $HOME|Lab (2026)"

# Each stub keeps its state as files in $STUB_STATE, so a test sets up a
# situation by creating files and reads the results from calls.log.
STUBS = {
    "launchctl": r"""
echo "launchctl $*" >> "$STUB_STATE/calls.log"
case "$1" in
  print) [ -e "$STUB_STATE/loaded" ] ;;
  bootstrap)
    if [ -e "$STUB_STATE/bootstrap_fails" ]; then echo "Bootstrap failed: 5: Input/output error" >&2; exit 5; fi
    touch "$STUB_STATE/loaded" ;;
  kickstart)
    [ -e "$STUB_STATE/loaded" ] || { echo "Could not find service" >&2; exit 113; }
    rm -f "$STUB_STATE/pid"
    [ -e "$STUB_STATE/server_never_answers" ] || touch "$STUB_STATE/healthy" ;;
  bootout)
    [ -e "$STUB_STATE/loaded" ] || exit 3
    rm -f "$STUB_STATE/loaded" ;;
esac
""",
    "curl": r"""
[ -e "$STUB_STATE/healthy" ] || exit 7
if [ -e "$STUB_STATE/pid" ] && ! kill -0 "$(cat "$STUB_STATE/pid")" 2>/dev/null; then exit 7; fi
echo '{"ok": true, "version": "1.1.0"}'
""",
    "lsof": r"""
if [ -e "$STUB_STATE/pid" ] && kill -0 "$(cat "$STUB_STATE/pid")" 2>/dev/null; then cat "$STUB_STATE/pid"; fi
""",
    "ps": r"""cat "$STUB_STATE/lstart" """,
    "open": r"""echo "open $*" >> "$STUB_STATE/calls.log" """,
    "osascript": r"""echo "osascript $*" >> "$STUB_STATE/calls.log" """,
    "sleep": "true",
    "sips": "true",
    "iconutil": "true",
    "defaults": r"""echo "defaults $*" >> "$STUB_STATE/calls.log"; [ "$1" != read ] """,
    "killall": r"""echo "killall $*" >> "$STUB_STATE/calls.log" """,
}


class Rig:
    """A copy of the project, a temporary home folder, and the stubs.

    Attributes:
        project: The copied project folder.
        home: The temporary home folder.
        state: The folder holding the stubs' state files.
    """

    def __init__(self, base: Path, folder_name: str) -> None:
        """Copy the installer and config into a temporary project and write the stubs.

        Args:
            base: An empty temporary folder.
            folder_name: The name of the project folder.
        """
        self.project = base / folder_name
        self.home = base / "home"
        self.state = base / "state"
        self.bin = base / "bin"
        for folder in (self.home, self.state, self.bin, self.project):
            folder.mkdir(parents=True)
        ignore = shutil.ignore_patterns("__pycache__")
        shutil.copytree(ROOT / "tools", self.project / "tools", ignore=ignore)
        shutil.copytree(ROOT / "screener", self.project / "screener", ignore=ignore)
        shutil.copytree(ROOT / "config", self.project / "config", ignore=ignore)
        shutil.copy(ROOT / "screener.py", self.project / "screener.py")
        # Drawing the icon takes seconds and has nothing to do with these tests.
        (self.project / "tools" / "make_icon.py").write_text("")
        for name, body in STUBS.items():
            path = self.bin / name
            path.write_text("#!/bin/bash\n" + body + "\n")
            path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def env(self) -> dict:
        """Return the environment that puts the stubs first and the temporary home in place."""
        return {
            **os.environ,
            "HOME": str(self.home),
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "STUB_STATE": str(self.state),
        }

    def install(self) -> subprocess.CompletedProcess:
        """Run the installer in the temporary project and return the finished process."""
        return subprocess.run(
            ["/bin/bash", str(self.project / "tools" / "install_dock_app.sh")],
            env=self.env(), capture_output=True, text=True, timeout=120,
        )

    @property
    def launcher(self) -> Path:
        """The launcher the installer writes."""
        return self.home / "Applications" / "Sponge Lab.app" / "Contents" / "MacOS" / "launch"

    @property
    def plist_path(self) -> Path:
        """The service definition the installer writes."""
        return self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    def plist(self) -> dict:
        """Read and return the service definition."""
        with self.plist_path.open("rb") as handle:
            return plistlib.load(handle)

    def launch(self) -> subprocess.CompletedProcess:
        """Run the launcher, as a click on the Dock icon does, and return the finished process."""
        return subprocess.run([str(self.launcher)], env=self.env(), capture_output=True, text=True, timeout=120)

    def calls(self) -> list:
        """Return the recorded stub calls, one string per call."""
        path = self.state / "calls.log"
        return path.read_text().splitlines() if path.exists() else []

    def set_state(self, *names: str) -> None:
        """Create the named state files.

        Args:
            names: The state file names, such as "loaded" or "healthy".
        """
        for name in names:
            (self.state / name).touch()

    def forget_install_calls(self) -> None:
        """Clear the calls the installer made, so a test sees only the launcher's."""
        (self.state / "calls.log").write_text("")

    def set_server_start(self, epoch_seconds: float) -> None:
        """Set the start time the ps stub reports for the server.

        Args:
            epoch_seconds: The start time in epoch seconds.
        """
        stamp = time.strftime("%a %b %d %H:%M:%S %Y", time.localtime(epoch_seconds))
        (self.state / "lstart").write_text(stamp + "\n")


@pytest.fixture
def rig(tmp_path):
    """Return an installed rig in an ordinary folder."""
    made = Rig(tmp_path, "SpongeScreener")
    result = made.install()
    assert result.returncode == 0, result.stderr
    made.forget_install_calls()
    return made


def service_target() -> str:
    """Return the launchd name of the service for the current user."""
    return f"gui/{os.getuid()}/{LABEL}"


def stand_in_server() -> int:
    """Start a process with no parent in this test, so it disappears as soon as it is killed.

    Returns:
        The process ID.
    """
    command = "/bin/sleep 60 >/dev/null 2>&1 & echo $!"
    return int(subprocess.check_output(["/bin/sh", "-c", command], text=True).strip())


def is_alive(pid: int) -> bool:
    """Return whether the process exists.

    Args:
        pid: The process ID.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def stop(pid: int) -> None:
    """Kill the process when it still exists.

    Args:
        pid: The process ID.
    """
    if is_alive(pid):
        os.kill(pid, signal.SIGKILL)


# The service definition


def test_service_runs_the_screener_in_the_project_folder(rig):
    plist = rig.plist()
    assert plist["Label"] == LABEL
    assert plist["ProgramArguments"] == ["/usr/bin/env", "python3", f"{rig.project}/screener.py", "--no-browser"]
    assert plist["WorkingDirectory"] == str(rig.project)


def test_service_appends_to_the_existing_log(rig):
    plist = rig.plist()
    log = f"{rig.project}/data/screener.log"
    assert plist["StandardOutPath"] == log
    assert plist["StandardErrorPath"] == log


def test_service_starts_only_when_asked_and_stays_stopped_when_stopped(rig):
    plist = rig.plist()
    assert plist.get("RunAtLoad", False) is False
    assert plist.get("KeepAlive", False) is False


def test_service_gets_full_priority_for_video(rig):
    assert rig.plist()["ProcessType"] == "Interactive"


def test_service_finds_homebrew_python_and_ffmpeg(rig):
    assert rig.plist()["EnvironmentVariables"]["PATH"].startswith("/opt/homebrew/bin:/usr/local/bin:")


# The launcher


def test_first_click_loads_and_starts_the_service_and_opens_chrome(rig):
    result = rig.launch()
    assert result.returncode == 0, result.stderr
    calls = rig.calls()
    assert f"launchctl bootstrap gui/{os.getuid()} {rig.plist_path}" in calls
    kickstart = f"launchctl kickstart {service_target()}"
    assert kickstart in calls
    assert calls.index(f"launchctl bootstrap gui/{os.getuid()} {rig.plist_path}") < calls.index(kickstart)
    assert calls[-1] == "open -a Google Chrome http://127.0.0.1:8770/"


def test_later_click_does_not_load_the_service_twice(rig):
    rig.set_state("loaded")
    assert rig.launch().returncode == 0
    calls = rig.calls()
    assert not [call for call in calls if call.startswith("launchctl bootstrap")]
    assert f"launchctl kickstart {service_target()}" in calls


def test_click_while_running_only_opens_chrome(rig):
    rig.set_state("loaded", "healthy")
    server = stand_in_server()
    try:
        (rig.state / "pid").write_text(str(server))
        rig.set_server_start(time.time() + 3600)
        assert rig.launch().returncode == 0
        assert is_alive(server)
    finally:
        stop(server)
    assert not [call for call in rig.calls() if "kickstart" in call or "bootstrap" in call]
    assert rig.calls()[-1] == "open -a Google Chrome http://127.0.0.1:8770/"


def test_server_older_than_the_code_is_replaced(rig):
    rig.set_state("loaded", "healthy")
    server = stand_in_server()
    try:
        (rig.state / "pid").write_text(str(server))
        rig.set_server_start(time.time() - 3600)
        os.utime(rig.project / "screener.py")
        assert rig.launch().returncode == 0
        assert not is_alive(server)
    finally:
        stop(server)
    assert f"launchctl kickstart {service_target()}" in rig.calls()


def test_alert_when_the_server_never_answers(rig):
    rig.set_state("server_never_answers")
    assert rig.launch().returncode == 1
    calls = rig.calls()
    assert any(call.startswith("osascript") and "did not start" in call for call in calls)
    assert not [call for call in calls if call.startswith("open")]


def test_service_refused_by_macos_is_written_to_the_log_and_alerted(rig):
    rig.set_state("bootstrap_fails")
    assert rig.launch().returncode == 1
    assert "Bootstrap failed: 5" in (rig.project / "data" / "screener.log").read_text()
    assert any(call.startswith("osascript") for call in rig.calls())


def test_launcher_never_starts_the_server_itself(rig):
    """The launcher exits right after Chrome opens, so macOS would stop any child it left."""
    text = rig.launcher.read_text()
    assert "nohup" not in text
    assert "screener.py\" --" not in text


# The installer


def test_reinstall_replaces_a_loaded_service(rig):
    rig.set_state("loaded")
    assert rig.install().returncode == 0
    assert f"launchctl bootout {service_target()}" in rig.calls()
    assert not (rig.state / "loaded").exists()


def test_install_succeeds_when_no_service_is_loaded(tmp_path):
    made = Rig(tmp_path, "SpongeScreener")
    result = made.install()
    assert result.returncode == 0, result.stderr
    assert made.plist_path.exists()


def test_install_has_no_placeholders_left(rig):
    assert "__" not in rig.launcher.read_text()


def test_awkward_folder_name_survives_in_the_service_and_the_launcher(tmp_path):
    made = Rig(tmp_path, AWKWARD_NAME)
    result = made.install()
    assert result.returncode == 0, result.stderr
    assert made.plist()["WorkingDirectory"] == str(made.project)
    assert subprocess.run(["/bin/bash", "-n", str(made.launcher)]).returncode == 0
    made.forget_install_calls()
    launched = made.launch()
    assert launched.returncode == 0, launched.stderr
    assert (made.project / "data").is_dir()
