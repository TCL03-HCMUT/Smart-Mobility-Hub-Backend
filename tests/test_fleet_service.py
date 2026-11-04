import pytest

from app.core.exceptions import AppError, NotFoundError
from app.models.entities import HubResource, PublicVehicle, Vehicle
from app.schemas.fleet import PrivateVehicleCreate, TelemetryCreate, VehicleStatusUpdate
from app.services.fleet_service import FleetService


# --- Private Vehicle Registration Tests ---

def test_register_private_vehicle_success(uow, seed_data):
    fleet_svc = FleetService(uow)
    payload = PrivateVehicleCreate(
        license_plate="59-A1 12345",
        current_hub_id=seed_data["hub_metro"].hub_id,
        vehicle_type="e-scooter",
        battery_pct=85.0,
        rfid_tag_code="RFID-001",
    )
    vehicle = fleet_svc.register_private_vehicle(seed_data["student"].user_id, payload)
    uow.commit()

    assert vehicle.vehicle_id is not None
    assert vehicle.license_plate == "59-A1 12345"
    assert vehicle.ownership_type == "PRIVATE"
    assert vehicle.status == "AVAILABLE"
    assert vehicle.battery_pct == 85.0

    private_veh = uow.private_vehicles.get_by_id(vehicle.vehicle_id)
    assert private_veh is not None
    assert private_veh.owner_id == seed_data["student"].user_id
    assert private_veh.rfid_tag_code == "RFID-001"


def test_register_private_vehicle_duplicate_plate(uow, seed_data):
    fleet_svc = FleetService(uow)
    payload = PrivateVehicleCreate(license_plate="59-B1 99999")
    fleet_svc.register_private_vehicle(seed_data["student"].user_id, payload)
    uow.commit()

    with pytest.raises(AppError) as exc_info:
        fleet_svc.register_private_vehicle(seed_data["student"].user_id, payload)
    assert exc_info.value.status_code == 409


def test_register_private_vehicle_owner_not_found(uow):
    fleet_svc = FleetService(uow)
    payload = PrivateVehicleCreate(license_plate="59-C1 11111")
    with pytest.raises(AppError) as exc_info:
        fleet_svc.register_private_vehicle("non-existent-user", payload)
    assert exc_info.value.status_code == 404


def test_register_private_vehicle_invalid_hub(uow, seed_data):
    fleet_svc = FleetService(uow)
    payload = PrivateVehicleCreate(
        license_plate="59-C2 12121",
        current_hub_id="non-existent-hub",
    )
    with pytest.raises(NotFoundError):
        fleet_svc.register_private_vehicle(seed_data["student"].user_id, payload)


def test_register_private_vehicle_battery_boundary_validation(uow, seed_data):
    fleet_svc = FleetService(uow)
    # Below 0%
    with pytest.raises(AppError) as exc:
        fleet_svc.register_private_vehicle(
            seed_data["student"].user_id,
            PrivateVehicleCreate(license_plate="59-C3 00001", battery_pct=-5.0),
        )
    assert exc.value.status_code == 400

    # Above 100%
    with pytest.raises(AppError) as exc:
        fleet_svc.register_private_vehicle(
            seed_data["student"].user_id,
            PrivateVehicleCreate(license_plate="59-C3 00002", battery_pct=105.0),
        )
    assert exc.value.status_code == 400

    # Boundary: exactly 0.0 and 100.0 are valid
    v0 = fleet_svc.register_private_vehicle(
        seed_data["student"].user_id,
        PrivateVehicleCreate(license_plate="59-C3 00003", battery_pct=0.0),
    )
    assert v0.battery_pct == 0.0

    v100 = fleet_svc.register_private_vehicle(
        seed_data["student"].user_id,
        PrivateVehicleCreate(license_plate="59-C3 00004", battery_pct=100.0),
    )
    assert v100.battery_pct == 100.0


# --- Vehicle Retrieval and Search Tests ---

def test_get_vehicle(uow, seed_data):
    fleet_svc = FleetService(uow)
    payload = PrivateVehicleCreate(license_plate="59-D1 22222")
    vehicle = fleet_svc.register_private_vehicle(seed_data["student"].user_id, payload)
    uow.commit()

    fetched = fleet_svc.get_vehicle(vehicle.vehicle_id)
    assert fetched.license_plate == "59-D1 22222"

    with pytest.raises(NotFoundError):
        fleet_svc.get_vehicle("non-existent-id")


def test_search_vehicles_multi_criteria(uow, seed_data):
    fleet_svc = FleetService(uow)
    hub_id = seed_data["hub_metro"].hub_id

    # Create various vehicles
    v1 = Vehicle(
        vehicle_id="v-search-1",
        license_plate="59-S1 001",
        current_hub_id=hub_id,
        vehicle_type="e-bike",
        ownership_type="PUBLIC",
        battery_pct=90.0,
        status="AVAILABLE",
    )
    v2 = Vehicle(
        vehicle_id="v-search-2",
        license_plate="59-S1 002",
        current_hub_id=hub_id,
        vehicle_type="e-scooter",
        ownership_type="PUBLIC",
        battery_pct=15.0,
        status="AVAILABLE",
    )
    v3 = Vehicle(
        vehicle_id="v-search-3",
        license_plate="59-S1 003",
        current_hub_id=seed_data["hub_dorm"].hub_id,
        vehicle_type="e-bike",
        ownership_type="PRIVATE",
        battery_pct=80.0,
        status="MAINTENANCE",
    )
    uow.vehicles.add(v1)
    uow.vehicles.add(v2)
    uow.vehicles.add(v3)
    uow.commit()

    # Search by hub
    res_hub = fleet_svc.search_vehicles({"hub_id": hub_id})
    res_ids = [v.vehicle_id for v in res_hub]
    assert "v-search-1" in res_ids and "v-search-2" in res_ids
    assert "v-search-3" not in res_ids

    # Search by vehicle_type
    res_type = fleet_svc.search_vehicles({"vehicle_type": "e-scooter"})
    assert [v.vehicle_id for v in res_type] == ["v-search-2"]

    # Search by ownership
    res_own = fleet_svc.search_vehicles({"ownership_type": "PRIVATE"})
    assert any(v.vehicle_id == "v-search-3" for v in res_own)

    # Search by min_battery
    res_bat = fleet_svc.search_vehicles({"min_battery": 50.0})
    bat_ids = [v.vehicle_id for v in res_bat]
    assert "v-search-1" in bat_ids
    assert "v-search-2" not in bat_ids


# --- Vehicle Status Update & Invariants Tests ---

def test_update_status_and_invariants(uow, seed_data):
    fleet_svc = FleetService(uow)
    payload = PrivateVehicleCreate(license_plate="59-E1 33333")
    vehicle = fleet_svc.register_private_vehicle(seed_data["student"].user_id, payload)
    uow.commit()

    # Valid status update
    fleet_svc.update_status(
        vehicle.vehicle_id,
        VehicleStatusUpdate(status="CHARGING", battery_pct=90.0),
    )
    assert vehicle.status == "CHARGING"
    assert vehicle.battery_pct == 90.0

    # Maintenance state
    fleet_svc.update_status(vehicle.vehicle_id, VehicleStatusUpdate(status="MAINTENANCE"))
    assert vehicle.status == "MAINTENANCE"

    # Direct transition from MAINTENANCE to RESERVED or IN_USE is prohibited (Rule 4)
    with pytest.raises(AppError) as exc:
        fleet_svc.update_status(vehicle.vehicle_id, VehicleStatusUpdate(status="RESERVED"))
    assert exc.value.status_code == 400

    with pytest.raises(AppError) as exc:
        fleet_svc.update_status(vehicle.vehicle_id, VehicleStatusUpdate(status="IN_USE"))
    assert exc.value.status_code == 400

    # Transition from MAINTENANCE to AVAILABLE is allowed
    fleet_svc.update_status(vehicle.vehicle_id, VehicleStatusUpdate(status="AVAILABLE"))
    assert vehicle.status == "AVAILABLE"


def test_update_status_invalid_inputs(uow, seed_data):
    fleet_svc = FleetService(uow)
    payload = PrivateVehicleCreate(license_plate="59-E2 44444")
    vehicle = fleet_svc.register_private_vehicle(seed_data["student"].user_id, payload)
    uow.commit()

    # Invalid status string
    with pytest.raises(AppError) as exc:
        fleet_svc.update_status(vehicle.vehicle_id, VehicleStatusUpdate(status="FLYING"))
    assert exc.value.status_code == 400

    # Invalid non-existent hub
    with pytest.raises(NotFoundError):
        fleet_svc.update_status(
            vehicle.vehicle_id,
            VehicleStatusUpdate(status="AVAILABLE", current_hub_id="bad-hub"),
        )

    # Invalid battery range
    with pytest.raises(AppError) as exc:
        fleet_svc.update_status(
            vehicle.vehicle_id,
            VehicleStatusUpdate(status="AVAILABLE", battery_pct=150.0),
        )
    assert exc.value.status_code == 400


# --- Telemetry Recording and Processing Tests ---

def test_record_telemetry_and_xor_constraint(uow, seed_data):
    fleet_svc = FleetService(uow)
    vehicle = Vehicle(
        vehicle_id="veh-telemetry-1",
        license_plate="59-F1 44444",
        ownership_type="PUBLIC",
        status="AVAILABLE",
        battery_pct=50.0,
    )
    uow.vehicles.add(vehicle)
    resource = HubResource(
        resource_id="res-1",
        hub_id=seed_data["hub_metro"].hub_id,
        code="PS-01",
        resource_type="PARKING_SPACE",
    )
    uow.resources.add(resource)
    uow.commit()

    # XOR constraint violation: both provided
    with pytest.raises(AppError):
        fleet_svc.record_telemetry(
            TelemetryCreate(vehicle_id="veh-telemetry-1", resource_id="res-1", metric_name="temp")
        )

    # XOR constraint violation: neither provided
    with pytest.raises(AppError):
        fleet_svc.record_telemetry(TelemetryCreate(metric_name="temp"))

    # Valid vehicle telemetry (updates vehicle battery_pct automatically)
    telem = fleet_svc.record_telemetry(
        TelemetryCreate(vehicle_id="veh-telemetry-1", metric_name="battery_pct", metric_value=72.0)
    )
    uow.commit()

    assert telem.sensor_id is not None
    assert vehicle.battery_pct == 72.0

    # Valid resource telemetry
    res_telem = fleet_svc.record_telemetry(
        TelemetryCreate(resource_id="res-1", metric_name="occupied", metric_value=1.0)
    )
    uow.commit()
    assert res_telem.sensor_id is not None
    assert res_telem.resource_id == "res-1"


def test_record_telemetry_missing_targets(uow):
    fleet_svc = FleetService(uow)
    # Non-existent vehicle
    with pytest.raises(NotFoundError):
        fleet_svc.record_telemetry(
            TelemetryCreate(vehicle_id="non-existent-veh", metric_name="speed", metric_value=25.0)
        )

    # Non-existent resource
    with pytest.raises(NotFoundError):
        fleet_svc.record_telemetry(
            TelemetryCreate(resource_id="non-existent-res", metric_name="power", metric_value=11.0)
        )


def test_process_telemetry_alerts(uow):
    fleet_svc = FleetService(uow)
    vehicle = Vehicle(
        vehicle_id="veh-alert-1",
        license_plate="59-G1 55555",
        ownership_type="PUBLIC",
        status="AVAILABLE",
        battery_pct=10.0,
    )
    uow.vehicles.add(vehicle)
    uow.commit()

    # Critical low battery (< 15%)
    t1 = fleet_svc.record_telemetry(
        TelemetryCreate(vehicle_id="veh-alert-1", metric_name="battery_pct", metric_value=12.0)
    )
    report1 = fleet_svc.process_telemetry(t1.sensor_id)
    assert "CRITICAL_BATTERY_LOW" in report1["alerts"]

    # Low battery for rental (< 20%)
    t2 = fleet_svc.record_telemetry(
        TelemetryCreate(vehicle_id="veh-alert-1", metric_name="battery_pct", metric_value=18.0)
    )
    report2 = fleet_svc.process_telemetry(t2.sensor_id)
    assert "BATTERY_LOW_UNAVAILABLE_FOR_RENTAL" in report2["alerts"]

    # Normal battery (>= 20%) -> no battery alert
    t3 = fleet_svc.record_telemetry(
        TelemetryCreate(vehicle_id="veh-alert-1", metric_name="battery_pct", metric_value=75.0)
    )
    report3 = fleet_svc.process_telemetry(t3.sensor_id)
    assert len(report3["alerts"]) == 0

    # Overheating warning (> 60C)
    t4 = fleet_svc.record_telemetry(
        TelemetryCreate(vehicle_id="veh-alert-1", metric_name="temperature", metric_value=68.5)
    )
    report4 = fleet_svc.process_telemetry(t4.sensor_id)
    assert "OVERHEATING_WARNING" in report4["alerts"]


# --- Maintenance Scheduling Tests ---

def test_schedule_maintenance(uow):
    fleet_svc = FleetService(uow)
    vehicle = Vehicle(
        vehicle_id="veh-pub-1",
        license_plate="59-H1 66666",
        ownership_type="PUBLIC",
        status="AVAILABLE",
    )
    uow.vehicles.add(vehicle)
    uow.session.flush()

    pub_veh = PublicVehicle(vehicle_id="veh-pub-1", fleet_batch_code="BATCH-2026")
    uow.public_vehicles.add(pub_veh)
    uow.commit()

    fleet_svc.schedule_maintenance("veh-pub-1", "2026-11-10T08:00:00Z")
    assert pub_veh.maintenance_schedule == "2026-11-10T08:00:00Z"
    assert vehicle.status == "MAINTENANCE"


def test_schedule_maintenance_private_vehicle_fails(uow, seed_data):
    fleet_svc = FleetService(uow)
    veh = fleet_svc.register_private_vehicle(
        seed_data["student"].user_id,
        PrivateVehicleCreate(license_plate="59-H2 77777"),
    )
    uow.commit()

    # Private vehicle cannot be scheduled for public fleet maintenance
    with pytest.raises(AppError) as exc:
        fleet_svc.schedule_maintenance(veh.vehicle_id, "2026-11-10T08:00:00Z")
    assert exc.value.status_code == 400


# --- Vehicle Redistribution Transit Tests ---

def test_mark_in_transit(uow, seed_data):
    fleet_svc = FleetService(uow)
    vehicle = Vehicle(
        vehicle_id="veh-transit-1",
        license_plate="59-T1 77777",
        current_hub_id=seed_data["hub_metro"].hub_id,
        ownership_type="PUBLIC",
        status="AVAILABLE",
    )
    uow.vehicles.add(vehicle)
    uow.commit()

    fleet_svc.mark_in_transit(
        "veh-transit-1",
        seed_data["hub_metro"].hub_id,
        seed_data["hub_dorm"].hub_id,
    )
    assert vehicle.status == "IN_TRANSIT"
    assert vehicle.current_hub_id is None


def test_mark_in_transit_wrong_origin(uow, seed_data):
    fleet_svc = FleetService(uow)
    vehicle = Vehicle(
        vehicle_id="veh-transit-2",
        license_plate="59-T2 88888",
        current_hub_id=seed_data["hub_dorm"].hub_id,
        ownership_type="PUBLIC",
        status="AVAILABLE",
    )
    uow.vehicles.add(vehicle)
    uow.commit()

    # Origin does not match vehicle.current_hub_id
    with pytest.raises(AppError) as exc:
        fleet_svc.mark_in_transit(
            "veh-transit-2",
            seed_data["hub_metro"].hub_id,
            seed_data["hub_dorm"].hub_id,
        )
    assert exc.value.status_code == 400


def test_mark_in_transit_invalid_status(uow, seed_data):
    fleet_svc = FleetService(uow)
    vehicle = Vehicle(
        vehicle_id="veh-transit-3",
        license_plate="59-T3 99999",
        current_hub_id=seed_data["hub_metro"].hub_id,
        ownership_type="PUBLIC",
        status="IN_USE",
    )
    uow.vehicles.add(vehicle)
    uow.commit()

    # Vehicle in use cannot be moved into transit
    with pytest.raises(AppError) as exc:
        fleet_svc.mark_in_transit(
            "veh-transit-3",
            seed_data["hub_metro"].hub_id,
            seed_data["hub_dorm"].hub_id,
        )
    assert exc.value.status_code == 400
