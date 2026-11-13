"""system_mammoth: read access of data paths per Unix group on a Slurm login node."""
import os
import shlex
import subprocess


def _probe(path, group):
    """Return True if `path` is readable (first byte of a file, or listable dir) as `group` ('' = current groups)."""
    q = shlex.quote(path)
    test = f"[ -d {q} ] && ls {q} >/dev/null 2>&1 || head -c1 {q} >/dev/null 2>&1"
    cmd = ["sg", group, "-c", test] if group else ["bash", "-c", test]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=60).returncode == 0
    except Exception:
        return False


def register(mcp):
    @mcp.tool()
    def mammoth_path_access(paths: list, groups: list = None) -> dict:
        """For each path report whether it is readable with the current groups and, one at a time, with each group in `groups` (via sg, which sees a group added after this shell started). Also reports the file owner and mode. Returns {status, rows: [{path, current, by_group: {g: bool}, owner, mode}]}."""
        rows = []
        for p in paths:
            try:
                st = os.stat(p)
                owner, mode = st.st_uid, oct(st.st_mode & 0o777)
            except OSError:
                owner, mode = None, None
            rows.append({"path": p, "current": _probe(p, ""),
                         "by_group": {g: _probe(p, g) for g in (groups or [])},
                         "owner": owner, "mode": mode})
        return {"status": "ok", "rows": rows}

    @mcp.tool()
    def mammoth_path_access_check(paths: list, groups: list = None) -> dict:
        """Pass when every path is readable with the current groups or one of `groups`. Returns {status: pass|fail, unreadable: [paths], need_session_restart: [paths readable only through sg]}."""
        r = mammoth_path_access(paths, groups)["rows"]
        bad = [x["path"] for x in r if not x["current"] and not any(x["by_group"].values())]
        late = [x["path"] for x in r if not x["current"] and any(x["by_group"].values())]
        return {"status": "fail" if bad else "pass", "unreadable": bad, "need_session_restart": late}
