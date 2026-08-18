from pa_host import jlink_usb


def test_windows_discovery_keeps_legacy_and_composite_probes_separate() -> None:
    rows = [
        {
            "instance_id": r"USB\VID_1366&PID_0101\000000123456",
            "friendly_name": "J-Link",
            "bus_description": "J-Link",
            "status": "Error",
            "problem_code": 28,
            "container_id": "legacy-container",
            "location": "Port_#0001.Hub_#0002",
        },
        {
            "instance_id": r"USB\VID_1366&PID_0105\000029734569",
            "friendly_name": "J-Link",
            "status": "OK",
            "problem_code": 0,
            "container_id": "new-container",
        },
        {
            "instance_id": r"USB\VID_1366&PID_0105&MI_02\000029734569",
            "friendly_name": "J-Link driver interface",
            "status": "Error",
            "problem_code": 28,
            "container_id": "new-container",
            "parent": r"USB\VID_1366&PID_0105\000029734569",
        },
    ]

    infos = sorted(jlink_usb._windows_infos(rows), key=lambda info: info.pid)

    assert [(item.pid, item.serial_number) for item in infos] == [
        (0x0101, "000000123456"),
        (0x0105, "000029734569"),
    ]
    assert infos[0].status == "Error"
    assert infos[0].problem_code == 28
    assert infos[1].interface == ""


def test_windows_discovery_uses_parent_serial_when_only_interface_is_visible() -> None:
    infos = jlink_usb._windows_infos([{
        "instance_id": r"USB\VID_1366&PID_0105&MI_02\7&ABC&0&0002",
        "friendly_name": "J-Link",
        "parent": r"USB\VID_1366&PID_0105\000029734569",
        "container_id": "new-container",
    }])

    assert len(infos) == 1
    assert infos[0].serial_number == "000029734569"
    assert infos[0].interface == "MI_02"


def test_windows_discovery_preserves_duplicate_serials_without_container_id() -> None:
    rows = [
        {
            "instance_id": r"USB\VID_1366&PID_0101\5&AAA&0&1",
            "serial_number": "000000123456",
            "friendly_name": "J-Link",
        },
        {
            "instance_id": r"USB\VID_1366&PID_0101\5&BBB&0&1",
            "serial_number": "000000123456",
            "friendly_name": "J-Link",
        },
    ]

    infos = jlink_usb._windows_infos(rows)

    assert len(infos) == 2
    assert {info.instance_id for info in infos} == {
        row["instance_id"] for row in rows
    }
    assert {info.serial_number for info in infos} == {"000000123456"}


def test_macos_ioreg_discovery_does_not_require_a_serial_port() -> None:
    payload = {
        "IORegistryEntryChildren": [{
            "IORegistryEntryName": "USB hub",
            "IORegistryEntryChildren": [{
                "IORegistryEntryID": 1234,
                "idVendor": 0x1366,
                "idProduct": 0x0101,
                "USB Product Name": "J-Link",
                "USB Vendor Name": "SEGGER",
                "USB Serial Number": "000000123456",
                "locationID": 0x01140000,
            }],
        }],
    }

    infos = jlink_usb._macos_infos(payload)

    assert len(infos) == 1
    assert infos[0].device == "ioreg:1234"
    assert infos[0].serial_number == "000000123456"
    assert infos[0].pid == 0x0101
    assert infos[0].location == "0x01140000"


def test_discovery_is_empty_on_unsupported_platform() -> None:
    assert jlink_usb.discover_jlink_usb_devices("linux") == []
