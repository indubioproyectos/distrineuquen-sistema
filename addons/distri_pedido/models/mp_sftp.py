"""Mercado Pago report fetched by SFTP (19.0.1.129.0, G10).

Mercado Pago delivers the scheduled report «Todas las transacciones» to an
SFTP server. When that server is this one, the files land in the folder of
distri_pedido.mp_carpeta_reportes and the existing cron (every 15 minutes,
`distri.mp.report._cron_import_folder`) imports them. When it is another
server, this fetcher copies the new reports into that folder first:

- cron «Mercado Pago: traer reportes por SFTP», disabled by default;
- ir.config_parameter: distri_pedido.mp_sftp_host, _port (22), _user, _path
  (the remote folder), _key_file (path of the private key inside the Odoo
  container, readable only by the odoo user) and, optionally,
  _known_hosts (a known_hosts file with the server's key: the host key is
  always checked, an unknown host is refused);
- transport: paramiko when installed, else the OpenSSH `sftp` client in
  batch mode. The image distri-odoo:19-ocr has neither yet: add
  `openssh-client` (apt) or `paramiko` (pip) to image/Dockerfile and mount
  the key file read only before enabling the cron.

Only .csv files directly in the remote folder are copied, once each (a name
already imported, or already in the local folder, «procesados» or
«errores», is skipped), and never larger than MAX_REPORT_BYTES. Nothing
secret is logged or stored: the key stays in its file.
"""
import logging
import os
import shlex
import shutil
import subprocess
import tempfile

from odoo import api, models

from .cobros import DONE_FOLDER, ERROR_FOLDER, MAX_REPORT_BYTES

_logger = logging.getLogger(__name__)

PARAM_PREFIX = "distri_pedido.mp_sftp_"
SFTP_TIMEOUT = 60


class SftpError(Exception):
    pass


class _ParamikoClient:
    def __init__(self, config):
        import paramiko  # optional dependency
        self._ssh = paramiko.SSHClient()
        self._ssh.load_system_host_keys()
        if config.get("known_hosts"):
            self._ssh.load_host_keys(config["known_hosts"])
        self._ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        self._ssh.connect(config["host"], port=config["port"], username=config["user"],
                          key_filename=config["key_file"], timeout=SFTP_TIMEOUT,
                          allow_agent=False, look_for_keys=False)
        self._sftp = self._ssh.open_sftp()

    def listdir(self, path):
        import stat
        return [(a.filename, a.st_size) for a in self._sftp.listdir_attr(path)
                if stat.S_ISREG(a.st_mode or 0)]

    def get(self, remote, local):
        self._sftp.get(remote, local)

    def close(self):
        self._sftp.close()
        self._ssh.close()


class _OpenSshClient:
    """The `sftp` command in batch mode, key only, host key checked."""

    def __init__(self, config):
        if not shutil.which("sftp"):
            raise SftpError("no SFTP client: install paramiko or openssh-client in the image")
        self._config = config

    def _run(self, commands):
        config = self._config
        args = ["sftp", "-b", "-", "-P", str(config["port"]), "-i", config["key_file"],
                "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "ConnectTimeout=%s" % SFTP_TIMEOUT]
        if config.get("known_hosts"):
            args += ["-o", "UserKnownHostsFile=%s" % config["known_hosts"]]
        args.append("%s@%s" % (config["user"], config["host"]))
        result = subprocess.run(args, input="\n".join(commands) + "\n", capture_output=True,
                                text=True, timeout=SFTP_TIMEOUT * 5, check=False)
        if result.returncode:
            raise SftpError("sftp exited with %s" % result.returncode)
        return result.stdout

    def listdir(self, path):
        out = self._run(["ls -ln %s" % shlex.quote(path)])
        files = []
        for line in out.splitlines():
            parts = line.split(None, 8)
            if len(parts) == 9 and parts[0].startswith("-"):
                try:
                    files.append((os.path.basename(parts[8]), int(parts[4])))
                except ValueError:
                    continue
        return files

    def get(self, remote, local):
        self._run(["get %s %s" % (shlex.quote(remote), shlex.quote(local))])

    def close(self):
        pass


class DistriMpReportSftp(models.Model):
    _inherit = "distri.mp.report"

    @api.model
    def _distri_sftp_config(self):
        """The parameters, or None when the fetcher is not configured."""
        get = self.env["ir.config_parameter"].sudo().get_param
        config = {key: (get(PARAM_PREFIX + key) or "").strip()
                  for key in ("host", "port", "user", "path", "key_file", "known_hosts")}
        if not all(config[k] for k in ("host", "user", "path", "key_file")):
            return None
        try:
            config["port"] = int(config["port"] or 22)
        except ValueError:
            config["port"] = 22
        return config

    @api.model
    def _distri_sftp_client(self, config):
        try:
            import paramiko  # noqa: F401
        except ImportError:
            return _OpenSshClient(config)
        return _ParamikoClient(config)

    @api.model
    def _distri_sftp_known(self, folder, name):
        """The report was already fetched or imported."""
        if self.sudo().search_count([("name", "=", name)]):
            return True
        return any(os.path.exists(os.path.join(folder, sub, name))
                   for sub in ("", DONE_FOLDER, ERROR_FOLDER))

    @api.model
    def _cron_sftp_fetch(self):
        """Copies the new reports of the remote folder into the local one,
        then imports the folder. Returns the names fetched."""
        config = self._distri_sftp_config()
        if not config:
            _logger.info("Mercado Pago SFTP: not configured (distri_pedido.mp_sftp_*)")
            return []
        folder = self._folder()
        os.makedirs(folder, exist_ok=True)
        fetched = []
        try:
            client = self._distri_sftp_client(config)
        except Exception as exc:
            _logger.warning("Mercado Pago SFTP: cannot connect: %s", type(exc).__name__)
            return []
        try:
            for name, size in sorted(client.listdir(config["path"])):
                if not name.lower().endswith(".csv") or "/" in name or name.startswith("."):
                    continue
                if self._distri_sftp_known(folder, name):
                    continue
                if size > MAX_REPORT_BYTES:
                    _logger.warning("Mercado Pago SFTP: %s is larger than 20 MB, skipped", name)
                    continue
                handle, partial = tempfile.mkstemp(prefix=".sftp-", dir=folder)
                os.close(handle)
                try:
                    client.get("%s/%s" % (config["path"].rstrip("/"), name), partial)
                    os.replace(partial, os.path.join(folder, name))
                    fetched.append(name)
                finally:
                    if os.path.exists(partial):
                        os.unlink(partial)
        except Exception as exc:
            _logger.warning("Mercado Pago SFTP: fetch failed: %s", type(exc).__name__)
        finally:
            try:
                client.close()
            except Exception:
                pass
        if fetched:
            _logger.info("Mercado Pago SFTP: %s reports fetched", len(fetched))
        self._cron_import_folder()
        return fetched
