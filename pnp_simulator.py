from __future__ import annotations

import argparse
import hashlib
import os
import re
import signal
import ssl
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

from cisco_ssh_simulator import (
    CiscoCommandSimulator,
    CommandResult,
    DeviceProfile,
    PROFILES,
    SimulatorServer,
)

PNP_NS = "urn:cisco:pnp"
WORK_INFO_NS = "urn:cisco:pnp:work-info"
DEVICE_INFO_NS = "urn:cisco:pnp:device-info"
CONFIG_UPGRADE_NS = "urn:cisco:pnp:config-upgrade"
IMAGE_INSTALL_NS = "urn:cisco:pnp:image-install"
BACKOFF_NS = "urn:cisco:pnp:backoff"

SERVICE_NAMESPACES = {
    DEVICE_INFO_NS: "device-info",
    CONFIG_UPGRADE_NS: "config-upgrade",
    IMAGE_INSTALL_NS: "image-install",
}

MAX_XML_BYTES = 256 * 1024
MAX_XML_ELEMENTS = 500
DEFAULT_MAX_CONFIG_BYTES = 1024 * 1024
DEFAULT_MAX_IMAGE_BYTES = 4 * 1024 * 1024 * 1024

ET.register_namespace("", PNP_NS)


class PnPSimulatorError(RuntimeError):
    pass


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: bytes
    headers: dict[str, str]


class Transport(Protocol):
    def get(self, url: str, *, max_bytes: int = MAX_XML_BYTES) -> HttpResult: ...

    def post(
        self, url: str, payload: bytes, *, max_bytes: int = MAX_XML_BYTES
    ) -> HttpResult: ...

    def download(self, url: str, *, max_bytes: int) -> bytes: ...


class HttpTransport:
    def __init__(
        self,
        *,
        timeout: float = 30.0,
        verify_tls: bool = True,
        ca_file: str | None = None,
    ):
        self.timeout = timeout
        if verify_tls:
            self.ssl_context = ssl.create_default_context(cafile=ca_file)
        else:
            self.ssl_context = ssl._create_unverified_context()

    def _request(self, request: Request, *, max_bytes: int) -> HttpResult:
        try:
            with urlopen(
                request, timeout=self.timeout, context=self.ssl_context
            ) as response:
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise PnPSimulatorError(
                        f"Remote response exceeds {max_bytes} bytes."
                    )
                return HttpResult(
                    status=response.status,
                    body=body,
                    headers={
                        key.lower(): value for key, value in response.headers.items()
                    },
                )
        except HTTPError as exc:
            exc.read(4096)
            raise PnPSimulatorError(f"Remote endpoint returned HTTP {exc.code}.") from exc
        except URLError as exc:
            raise PnPSimulatorError(f"Unable to reach remote endpoint: {exc.reason}.") from exc

    def get(self, url: str, *, max_bytes: int = MAX_XML_BYTES) -> HttpResult:
        return self._request(
            Request(url, method="GET", headers={"Accept": "application/xml"}),
            max_bytes=max_bytes,
        )

    def post(
        self, url: str, payload: bytes, *, max_bytes: int = MAX_XML_BYTES
    ) -> HttpResult:
        return self._request(
            Request(
                url,
                data=payload,
                method="POST",
                headers={
                    "Accept": "application/xml",
                    "Content-Type": "application/xml",
                    "User-Agent": "ZAS-PnP-Simulator/1.0",
                },
            ),
            max_bytes=max_bytes,
        )

    def download(self, url: str, *, max_bytes: int) -> bytes:
        return self.get(url, max_bytes=max_bytes).body


@dataclass(frozen=True)
class DeviceIdentity:
    product_id: str = "C9300-48P"
    version_id: str = "V01"
    serial_number: str = "FCW1234ABCD"
    hostname: str = "Switch"
    software_version: str = "17.09.05"

    def __post_init__(self) -> None:
        values = {
            "product ID": self.product_id,
            "version ID": self.version_id,
            "serial number": self.serial_number,
            "hostname": self.hostname,
        }
        for label, value in values.items():
            if (
                not value
                or len(value) > 100
                or any(char in value for char in "<>,\r\n")
            ):
                raise ValueError(f"Invalid {label}.")

    @property
    def udi(self) -> str:
        return f"PID:{self.product_id},VID:{self.version_id},SN:{self.serial_number.upper()}"


@dataclass(frozen=True)
class ParsedMessage:
    kind: str
    correlator: str
    root: ET.Element
    body: ET.Element
    credentials: tuple[str, str] | None = None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _namespace(tag: str) -> str:
    if tag.startswith("{"):
        return tag[1:].split("}", 1)[0]
    return ""


def _xml(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _root(identity: DeviceIdentity) -> ET.Element:
    return ET.Element(ET.QName(PNP_NS, "pnp"), {"version": "1.0", "udi": identity.udi})


def build_work_request(identity: DeviceIdentity, correlator: str) -> bytes:
    root = _root(identity)
    info = ET.SubElement(
        root, ET.QName(WORK_INFO_NS, "info"), {"correlator": correlator}
    )
    device_id = ET.SubElement(info, ET.QName(WORK_INFO_NS, "deviceId"))
    ET.SubElement(device_id, ET.QName(WORK_INFO_NS, "udi")).text = identity.udi
    ET.SubElement(device_id, ET.QName(WORK_INFO_NS, "hostname")).text = (
        identity.hostname
    )
    ET.SubElement(device_id, ET.QName(WORK_INFO_NS, "authRequired")).text = "true"
    ET.SubElement(device_id, ET.QName(WORK_INFO_NS, "viaProxy")).text = "false"
    return _xml(root)


def build_service_response(
    identity: DeviceIdentity,
    *,
    service: str,
    namespace: str,
    correlator: str,
    success: bool,
    error_message: str = "",
) -> bytes:
    root = _root(identity)
    response = ET.SubElement(
        root,
        ET.QName(namespace, "response"),
        {"correlator": correlator, "success": "1" if success else "0"},
    )
    if success and service == "device-info":
        udi = ET.SubElement(response, ET.QName(namespace, "udi"))
        ET.SubElement(udi, ET.QName(namespace, "primary-chassis")).text = identity.udi
        image_info = ET.SubElement(response, ET.QName(namespace, "imageInfo"))
        ET.SubElement(image_info, ET.QName(namespace, "versionString")).text = (
            identity.software_version
        )
        hardware_info = ET.SubElement(response, ET.QName(namespace, "hardwareInfo"))
        ET.SubElement(hardware_info, ET.QName(namespace, "hostname")).text = (
            identity.hostname
        )
        ET.SubElement(hardware_info, ET.QName(namespace, "vendor")).text = "Cisco"
        ET.SubElement(hardware_info, ET.QName(namespace, "platformName")).text = (
            identity.product_id
        )
        ET.SubElement(hardware_info, ET.QName(namespace, "boardId")).text = (
            identity.serial_number.upper()
        )
    elif not success:
        error_info = ET.SubElement(response, ET.QName(namespace, "errorInfo"))
        ET.SubElement(error_info, ET.QName(namespace, "errorSeverity")).text = "ERROR"
        ET.SubElement(error_info, ET.QName(namespace, "errorCode")).text = (
            "SIMULATOR_ERROR"
        )
        ET.SubElement(error_info, ET.QName(namespace, "errorMessage")).text = (
            error_message[:512]
        )
    return _xml(root)


def parse_server_message(payload: bytes, *, expected_udi: str) -> ParsedMessage:
    if not payload or len(payload) > MAX_XML_BYTES:
        raise PnPSimulatorError("PnP XML response is empty or too large.")
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise PnPSimulatorError("Unsafe PnP XML response.")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise PnPSimulatorError("Malformed PnP XML response.") from exc
    if sum(1 for _ in root.iter()) > MAX_XML_ELEMENTS:
        raise PnPSimulatorError("PnP XML response contains too many elements.")
    if (
        _local_name(root.tag) != "pnp"
        or _namespace(root.tag) != PNP_NS
        or root.attrib.get("version") != "1.0"
    ):
        raise PnPSimulatorError("Unsupported PnP XML response root.")
    if root.attrib.get("udi") != expected_udi:
        raise PnPSimulatorError("PnP response UDI does not match this simulator.")
    children = list(root)
    if len(children) != 1:
        raise PnPSimulatorError("PnP response must contain exactly one operation.")
    body = children[0]
    correlator = (body.attrib.get("correlator") or "").strip()
    if not correlator:
        raise PnPSimulatorError("PnP response correlator is missing.")
    namespace = _namespace(body.tag)
    local_name = _local_name(body.tag)
    if local_name == "request" and namespace in SERVICE_NAMESPACES:
        kind = SERVICE_NAMESPACES[namespace]
    elif local_name == "request" and namespace == BACKOFF_NS:
        kind = "backoff"
    elif local_name == "info" and namespace == WORK_INFO_NS:
        if not any(_local_name(node.tag) == "bye" for node in body.iter()):
            raise PnPSimulatorError("Unsupported PnP work-info response.")
        kind = "bye"
    else:
        raise PnPSimulatorError("Unsupported PnP server operation.")
    username = root.attrib.get("usr")
    password = root.attrib.get("pwd")
    credentials = (
        (username, password) if username is not None and password is not None else None
    )
    return ParsedMessage(
        kind=kind,
        correlator=correlator,
        root=root,
        body=body,
        credentials=credentials,
    )


def _child_text(parent: ET.Element, name: str) -> str:
    for node in parent.iter():
        if _local_name(node.tag) == name:
            return (node.text or "").strip()
    return ""


def _backoff_seconds(body: ET.Element) -> int:
    if any(_local_name(node.tag) == "terminate" for node in body.iter()):
        return -1
    try:
        hours = int(_child_text(body, "hours") or 0)
        minutes = int(_child_text(body, "minutes") or 0)
        seconds = int(_child_text(body, "seconds") or 0)
    except ValueError as exc:
        raise PnPSimulatorError("Backoff response contains an invalid duration.") from exc
    if not (hours or minutes or seconds):
        try:
            minutes = int(_child_text(body, "defaultMinutes") or 15)
        except ValueError as exc:
            raise PnPSimulatorError(
                "Backoff response contains an invalid duration."
            ) from exc
    return max(0, hours * 3600 + minutes * 60 + seconds)


def _checksum_algorithm(checksum: str) -> str:
    algorithms = {32: "md5", 40: "sha1", 64: "sha256", 128: "sha512"}
    algorithm = algorithms.get(len(checksum))
    if not algorithm or not re.fullmatch(r"[0-9a-fA-F]+", checksum):
        raise PnPSimulatorError("Unsupported image checksum format.")
    return algorithm


class EmbeddedSSHSimulator:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
    ):
        self.server = SimulatorServer(
            platform="iosxe",
            host=host,
            port=port,
            username=username,
            password=password,
        )

    def start(self) -> None:
        self.server.start()

    def stop(self) -> None:
        self.server.stop()

    def update_credentials(self, username: str, password: str) -> None:
        self.server.username = username
        self.server.password = password

    def apply_configuration(self, payload: bytes) -> None:
        config = payload.decode("utf-8", errors="replace")
        hostname_match = re.search(
            r"^hostname\s+(?P<hostname>\S+)\s*$", config, flags=re.MULTILINE
        )
        hostname = (
            hostname_match.group("hostname")
            if hostname_match
            else self.server.profile.hostname
        )
        command_table = dict(PROFILES["iosxe"].command_table)
        result = CommandResult(stdout=config.rstrip() + "\n")
        for command in ("show running-config", "show run", "sh run"):
            command_table[command] = result
        command_table["hostname"] = CommandResult(stdout=f"{hostname}\n")
        profile = DeviceProfile(
            key="iosxe",
            hostname=hostname,
            prompt=f"{hostname}#",
            command_table=command_table,
        )
        self.server.profile = profile
        self.server.simulator = CiscoCommandSimulator(profile)


class PnPAgent:
    def __init__(
        self,
        *,
        server_url: str,
        identity: DeviceIdentity,
        transport: Transport,
        output_dir: Path,
        poll_interval: float = 5.0,
        max_backoff: float = 60.0,
        max_config_bytes: int = DEFAULT_MAX_CONFIG_BYTES,
        max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
        sleeper: Callable[[float], None] = time.sleep,
        on_credentials: Callable[[str, str], None] | None = None,
        on_configuration: Callable[[bytes], None] | None = None,
        fail_service: str | None = None,
        log: Callable[[str], None] = print,
    ):
        parsed_url = urlparse(server_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("server_url must be an absolute HTTP(S) URL.")
        self.base_url = server_url.rstrip("/")
        if not self.base_url.endswith("/pnp"):
            self.base_url += "/pnp"
        self.identity = identity
        self.transport = transport
        self.output_dir = output_dir
        self.poll_interval = max(0.0, poll_interval)
        self.max_backoff = max(0.0, max_backoff)
        self.max_config_bytes = max_config_bytes
        self.max_image_bytes = max_image_bytes
        self.sleeper = sleeper
        self.on_credentials = on_credentials
        self.on_configuration = on_configuration
        self.fail_service = fail_service
        self.log = log
        self.stop_event = threading.Event()
        self.applied_configuration: Path | None = None
        self.downloaded_image: Path | None = None

    def stop(self) -> None:
        self.stop_event.set()

    def hello(self) -> None:
        result = self.transport.get(f"{self.base_url}/HELLO")
        if result.status != 200:
            raise PnPSimulatorError(f"HELLO returned HTTP {result.status}.")
        self.log("PnP HELLO succeeded.")

    def _poll(self) -> ParsedMessage:
        correlator = f"CiscoPnP-1.0-{uuid.uuid4().hex[:20]}"
        payload = build_work_request(self.identity, correlator)
        result = self.transport.post(f"{self.base_url}/WORK-REQUEST", payload)
        if result.status != 200:
            raise PnPSimulatorError(f"WORK-REQUEST returned HTTP {result.status}.")
        return parse_server_message(result.body, expected_udi=self.identity.udi)

    def _post_response(
        self, message: ParsedMessage, *, success: bool, error: str = ""
    ) -> None:
        namespace = _namespace(message.body.tag)
        payload = build_service_response(
            self.identity,
            service=message.kind,
            namespace=namespace,
            correlator=message.correlator,
            success=success,
            error_message=error,
        )
        result = self.transport.post(f"{self.base_url}/WORK-RESPONSE", payload)
        if result.status != 200:
            raise PnPSimulatorError(f"WORK-RESPONSE returned HTTP {result.status}.")
        acknowledgement = parse_server_message(
            result.body, expected_udi=self.identity.udi
        )
        if acknowledgement.kind != "bye":
            raise PnPSimulatorError("WORK-RESPONSE was not acknowledged with bye.")

    def _apply_credentials(self, message: ParsedMessage) -> None:
        if message.credentials and self.on_credentials:
            self.on_credentials(*message.credentials)

    def _handle_config(self, message: ParsedMessage) -> None:
        location = _child_text(message.body, "location")
        if not location:
            raise PnPSimulatorError("Config-upgrade request has no source location.")
        payload = self.transport.download(location, max_bytes=self.max_config_bytes)
        if not payload:
            raise PnPSimulatorError("Downloaded configuration is empty.")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        destination = self.output_dir / "startup-config.txt"
        temporary = self.output_dir / ".startup-config.txt.tmp"
        temporary.write_bytes(payload)
        os.replace(temporary, destination)
        self.applied_configuration = destination
        if self.on_configuration:
            self.on_configuration(payload)
        self.log(f"Applied configuration to {destination}.")

    def _handle_image(self, message: ParsedMessage) -> None:
        location = _child_text(message.body, "location")
        checksum = _child_text(message.body, "checksum")
        if not location:
            raise PnPSimulatorError("Image-install request has no source location.")
        payload = self.transport.download(location, max_bytes=self.max_image_bytes)
        if checksum:
            algorithm = _checksum_algorithm(checksum)
            if hashlib.new(algorithm, payload).hexdigest().lower() != checksum.lower():
                raise PnPSimulatorError("Downloaded image checksum does not match.")
        filename = Path(urlparse(location).path).name or "pnp-image.bin"
        filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        destination = self.output_dir / filename
        temporary = self.output_dir / f".{filename}.tmp"
        temporary.write_bytes(payload)
        os.replace(temporary, destination)
        self.downloaded_image = destination
        self.log(f"Downloaded and verified image at {destination}.")

    def run(
        self, *, max_polls: int | None = None, exit_on_backoff: bool = False
    ) -> str:
        self.hello()
        polls = 0
        while not self.stop_event.is_set():
            if max_polls is not None and polls >= max_polls:
                return "poll-limit"
            polls += 1
            message = self._poll()
            self._apply_credentials(message)
            self.log(f"Received PnP operation: {message.kind}.")
            if message.kind == "bye":
                return "complete"
            if message.kind == "backoff":
                seconds = _backoff_seconds(message.body)
                if seconds < 0:
                    return "terminated"
                if exit_on_backoff:
                    return "backoff"
                self.sleeper(min(float(seconds), self.max_backoff))
                continue

            error = ""
            try:
                if self.fail_service == message.kind:
                    raise PnPSimulatorError(
                        f"Simulated {message.kind} service failure."
                    )
                if message.kind == "config-upgrade":
                    self._handle_config(message)
                elif message.kind == "image-install":
                    self._handle_image(message)
            except (OSError, PnPSimulatorError) as exc:
                error = str(exc)
            self._post_response(message, success=not error, error=error)
            if error:
                raise PnPSimulatorError(error)
            if self.poll_interval:
                self.sleeper(self.poll_interval)
        return "stopped"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Simulate a Cisco IOS-XE PnP agent against a ZAS server."
    )
    parser.add_argument("--server", required=True, help="ZAS origin or /pnp base URL")
    parser.add_argument("--product-id", default="C9300-48P")
    parser.add_argument("--version-id", default="V01")
    parser.add_argument("--serial", default="FCW1234ABCD")
    parser.add_argument("--hostname", default="Switch")
    parser.add_argument("--software-version", default="17.09.05")
    parser.add_argument("--output-dir", type=Path, default=Path("pnp-artifacts"))
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--max-backoff", type=float, default=60.0)
    parser.add_argument("--max-polls", type=int)
    parser.add_argument("--exit-on-backoff", action="store_true")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--ca-file")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification (isolated development only)",
    )
    parser.add_argument(
        "--fail-service",
        choices=("device-info", "image-install", "config-upgrade"),
    )
    parser.add_argument(
        "--ssh-port",
        type=int,
        help="Also run the IOS-XE SSH simulator for the ZAS handoff",
    )
    parser.add_argument("--ssh-host", default="127.0.0.1")
    parser.add_argument("--ssh-username", default="admin")
    parser.add_argument("--ssh-password", default="admin")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    identity = DeviceIdentity(
        product_id=args.product_id,
        version_id=args.version_id,
        serial_number=args.serial,
        hostname=args.hostname,
        software_version=args.software_version,
    )
    ssh_simulator = None
    if args.ssh_port is not None:
        ssh_simulator = EmbeddedSSHSimulator(
            host=args.ssh_host,
            port=args.ssh_port,
            username=args.ssh_username,
            password=args.ssh_password,
        )
        ssh_simulator.start()
        print(
            f"IOS-XE SSH simulator listening on {args.ssh_host}:{ssh_simulator.server.port}."
        )
    agent = PnPAgent(
        server_url=args.server,
        identity=identity,
        transport=HttpTransport(
            timeout=args.timeout,
            verify_tls=not args.insecure,
            ca_file=args.ca_file,
        ),
        output_dir=args.output_dir,
        poll_interval=args.poll_interval,
        max_backoff=args.max_backoff,
        on_credentials=ssh_simulator.update_credentials if ssh_simulator else None,
        on_configuration=ssh_simulator.apply_configuration if ssh_simulator else None,
        fail_service=args.fail_service,
    )

    def stop(_signum, _frame):
        agent.stop()

    signal.signal(signal.SIGTERM, stop)
    try:
        result = agent.run(
            max_polls=args.max_polls, exit_on_backoff=args.exit_on_backoff
        )
        print(f"PnP simulator finished: {result}.")
        return 0
    except (PnPSimulatorError, ValueError) as exc:
        print(f"PnP simulator failed: {exc}")
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if ssh_simulator:
            ssh_simulator.stop()


if __name__ == "__main__":
    raise SystemExit(main())
