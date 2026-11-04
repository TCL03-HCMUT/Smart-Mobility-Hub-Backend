from app.models.entities import PublicVehicle, Vehicle


def test_api_rental_lifecycle(client, uow, seed_data):
    # Seed available public vehicle
    veh = Vehicle(
        vehicle_id="veh-api-rent-1",
        license_plate="59-API-RENT",
        current_hub_id=seed_data["hub_metro"].hub_id,
        vehicle_type="e-bike",
        ownership_type="PUBLIC",
        battery_pct=90.0,
        status="AVAILABLE",
    )
    uow.vehicles.add(veh)
    uow.session.flush()

    pub = PublicVehicle(vehicle_id="veh-api-rent-1")
    uow.public_vehicles.add(pub)
    uow.commit()

    token = seed_data["student"].user_id
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Create reservation
    res_resp = client.post(
        "/api/v1/rentals/reservations",
        headers=headers,
        json={"vehicle_id": "veh-api-rent-1"},
    )
    assert res_resp.status_code == 201
    res_data = res_resp.json()
    reservation_id = res_data["reservation_id"]
    assert res_data["status"] == "CONFIRMED"

    # 2. Pick up vehicle
    pickup_resp = client.post(
        f"/api/v1/rentals/reservations/{reservation_id}/pickup",
        headers=headers,
    )
    assert pickup_resp.status_code == 200
    rental_data = pickup_resp.json()
    rental_id = rental_data["rental_id"]
    assert rental_data["status"] == "ACTIVE"

    # 3. Return vehicle
    return_resp = client.post(
        f"/api/v1/rentals/rentals/{rental_id}/return",
        headers=headers,
        json={
            "return_hub_id": seed_data["hub_dorm"].hub_id,
            "return_condition": "GOOD",
        },
    )
    assert return_resp.status_code == 200
    assert return_resp.json()["status"] == "COMPLETED"


def test_api_cancel_reservation(client, uow, seed_data):
    veh = Vehicle(
        vehicle_id="veh-api-rent-2",
        license_plate="59-API-RENT-2",
        current_hub_id=seed_data["hub_metro"].hub_id,
        vehicle_type="e-bike",
        ownership_type="PUBLIC",
        battery_pct=90.0,
        status="AVAILABLE",
    )
    uow.vehicles.add(veh)
    uow.session.flush()

    uow.public_vehicles.add(PublicVehicle(vehicle_id="veh-api-rent-2"))
    uow.commit()

    token = seed_data["student"].user_id
    headers = {"Authorization": f"Bearer {token}"}

    res_resp = client.post(
        "/api/v1/rentals/reservations",
        headers=headers,
        json={"vehicle_id": "veh-api-rent-2"},
    )
    reservation_id = res_resp.json()["reservation_id"]

    # Unauthorized student cancels -> 403
    unauth_resp = client.post(
        f"/api/v1/rentals/reservations/{reservation_id}/cancel",
        headers={"Authorization": f"Bearer {seed_data['student2'].user_id}"},
    )
    assert unauth_resp.status_code == 403

    # Authorized cancellation
    cancel_resp = client.post(
        f"/api/v1/rentals/reservations/{reservation_id}/cancel",
        headers=headers,
    )
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "CANCELLED"


def test_api_rental_conflict_responses(client, uow, seed_data):
    veh = Vehicle(
        vehicle_id="veh-api-conflict",
        license_plate="59-API-CONFLICT",
        current_hub_id=seed_data["hub_metro"].hub_id,
        vehicle_type="e-bike",
        ownership_type="PUBLIC",
        battery_pct=90.0,
        status="AVAILABLE",
    )
    uow.vehicles.add(veh)
    uow.session.flush()
    uow.public_vehicles.add(PublicVehicle(vehicle_id="veh-api-conflict"))
    uow.commit()

    token1 = seed_data["student"].user_id
    token2 = seed_data["student2"].user_id

    # User 1 reserves
    client.post(
        "/api/v1/rentals/reservations",
        headers={"Authorization": f"Bearer {token1}"},
        json={"vehicle_id": "veh-api-conflict"},
    )

    # User 2 attempts double-booking -> 409 Conflict
    conflict_resp = client.post(
        "/api/v1/rentals/reservations",
        headers={"Authorization": f"Bearer {token2}"},
        json={"vehicle_id": "veh-api-conflict"},
    )
    assert conflict_resp.status_code == 409
