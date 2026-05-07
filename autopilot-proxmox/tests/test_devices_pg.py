def test_upsert_and_group_devices_by_serial(pg_conn):
    from web import devices_pg

    devices_pg.reset_for_tests(pg_conn)
    devices_pg.init(pg_conn)
    assert devices_pg.upsert_autopilot([
        {"id": "ap-1", "serialNumber": "ABC123", "groupTag": "lab"}
    ]) == 1
    assert devices_pg.upsert_intune([
        {"id": "mdm-1", "serialNumber": "ABC123", "deviceName": "Gell-ABC123"}
    ]) == 1
    assert devices_pg.upsert_entra([
        {
            "id": "aad-1",
            "deviceId": "dev-1",
            "displayName": "Gell-ABC123",
            "physicalIds": ["[SerialNumber]:ABC123", "[ZTDID]:ztd-1"],
        }
    ]) == 1

    grouped = devices_pg.grouped_by_serial()

    assert grouped["ABC123"]["autopilot"]["id"] == "ap-1"
    assert grouped["ABC123"]["intune"]["id"] == "mdm-1"
    assert grouped["ABC123"]["entra"][0]["id"] == "aad-1"
    assert grouped["ABC123"]["entra"][0]["ztdid"] == "ztd-1"


def test_upserts_replace_each_cache(pg_conn):
    from web import devices_pg

    devices_pg.reset_for_tests(pg_conn)
    devices_pg.init(pg_conn)
    devices_pg.upsert_autopilot([
        {"id": "old-ap", "serialNumber": "OLD"},
    ])
    devices_pg.upsert_autopilot([
        {"id": "new-ap", "serialNumber": "NEW"},
    ])

    grouped = devices_pg.grouped_by_serial()

    assert "OLD" not in grouped
    assert grouped["NEW"]["autopilot"]["id"] == "new-ap"


def test_unmatched_entra_excludes_intune_device_id_matches(pg_conn):
    from web import devices_pg

    devices_pg.reset_for_tests(pg_conn)
    devices_pg.init(pg_conn)
    devices_pg.upsert_intune([
        {
            "id": "mdm-1",
            "serialNumber": "ABC123",
            "azureADDeviceId": "dev-1",
        }
    ])
    devices_pg.upsert_entra([
        {"id": "matched", "deviceId": "dev-1", "displayName": "Matched"},
        {"id": "unmatched", "deviceId": "dev-2", "displayName": "Unmatched"},
    ])

    assert [row["id"] for row in devices_pg.list_unmatched_entra()] == ["unmatched"]
    grouped = devices_pg.grouped_by_serial()
    assert grouped["ABC123"]["entra"][0]["id"] == "matched"


def test_deletions_return_newest_first(pg_conn):
    from web import devices_pg

    devices_pg.reset_for_tests(pg_conn)
    devices_pg.init(pg_conn)
    first = devices_pg.record_deletion(
        "intune", "old-id", serial="OLD", status="ok"
    )
    second = devices_pg.record_deletion(
        "entra", "new-id", display_name="New Device", status="error",
        message="denied"
    )

    deletions = devices_pg.list_deletions(limit=2)

    assert first["object_id"] == "old-id"
    assert second["message"] == "denied"
    assert [row["object_id"] for row in deletions] == ["new-id", "old-id"]
