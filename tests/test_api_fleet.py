def test_api_register_private_vehicle(client, seed_data):
    token = seed_data["student"].user_id
    response = client.post(
        "/api/v1/fleet/vehicles/private",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "license_plate": "59-API-001",
            "current_hub_id": seed_data["hub_metro"].hub_id,
            "vehicle_type": "e-scooter",
            "battery_pct": 95.0,
            "rfid_tag_code": "RFID-API",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["license_plate"] == "59-API-001"
    assert data["ownership_type"] == "PRIVATE"


def test_api_register_private_vehicle_unauthenticated(client):
    # Attempt without Bearer token -> 401
    response = client.post(
        "/api/v1/fleet/vehicles/private",
        json={"license_plate": "59-NO-AUTH"},
    )
    assert response.status_code == 401


def test_api_register_duplicate_license_plate_fails(client, seed_data):
    token = seed_data["student"].user_id
    headers = {"Authorization": f"Bearer {token}"}
    client.post(
        "/api/v1/fleet/vehicles/private",
        headers=headers,
        json={"license_plate": "59-DUP-PLATE"},
    )
    duplicate_resp = client.post(
        "/api/v1/fleet/vehicles/private",
        headers=headers,
        json={"license_plate": "59-DUP-PLATE"},
    )
    assert duplicate_resp.status_code == 409


def test_api_update_vehicle_status(client, seed_data):
    token = seed_data["student"].user_id
    reg_resp = client.post(
        "/api/v1/fleet/vehicles/private",
        headers={"Authorization": f"Bearer {token}"},
        json={"license_plate": "59-API-002", "battery_pct": 50.0},
    )
    vehicle_id = reg_resp.json()["vehicle_id"]

    # Valid status update
    patch_resp = client.patch(
        f"/api/v1/fleet/vehicles/{vehicle_id}/status",
        json={"status": "CHARGING", "battery_pct": 80.0},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["status"] == "CHARGING"
    assert patch_resp.json()["battery_pct"] == 80.0

    # Invalid status value -> 400
    bad_status_resp = client.patch(
        f"/api/v1/fleet/vehicles/{vehicle_id}/status",
        json={"status": "DISINTEGRATED"},
    )
    assert bad_status_resp.status_code == 400


def test_api_record_telemetry(client, seed_data):
    token = seed_data["student"].user_id
    reg_resp = client.post(
        "/api/v1/fleet/vehicles/private",
        headers={"Authorization": f"Bearer {token}"},
        json={"license_plate": "59-API-003", "battery_pct": 50.0},
    )
    vehicle_id = reg_resp.json()["vehicle_id"]

    # Post telemetry
    telem_resp = client.post(
        "/api/v1/fleet/telemetry",
        json={
            "vehicle_id": vehicle_id,
            "metric_name": "battery_pct",
            "metric_value": 77.0,
            "is_simulated": True,
        },
    )
    assert telem_resp.status_code == 201
    assert telem_resp.json()["metric_name"] == "battery_pct"

    # Telemetry XOR constraint violation (no vehicle_id and no resource_id) -> 400
    bad_telem = client.post(
        "/api/v1/fleet/telemetry",
        json={"metric_name": "speed", "metric_value": 20.0},
    )
    assert bad_telem.status_code == 400
