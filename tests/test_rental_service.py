from datetime import UTC, datetime, timedelta
import pytest

from app.core.exceptions import AppError, NotFoundError
from app.models.entities import PublicVehicle, Vehicle
from app.schemas.rental import VehicleReservationCreate
from app.services.rental_service import RentalService


@pytest.fixture
def available_public_vehicle(uow, seed_data):
    vehicle = Vehicle(
        vehicle_id="veh-rent-1",
        license_plate="59-R1 10001",
        current_hub_id=seed_data["hub_metro"].hub_id,
        vehicle_type="e-bike",
        ownership_type="PUBLIC",
        battery_pct=80.0,
        status="AVAILABLE",
    )
    uow.vehicles.add(vehicle)
    uow.session.flush()

    public_veh = PublicVehicle(vehicle_id="veh-rent-1", fleet_batch_code="BATCH-01")
    uow.public_vehicles.add(public_veh)
    uow.commit()
    return vehicle


# --- Trip Planning Tests ---

def test_plan_and_cancel_trip(uow, seed_data):
    rental_svc = RentalService(uow)
    trip = rental_svc.plan_trip(
        seed_data["student"].user_id,
        {
            "origin_hub_id": seed_data["hub_metro"].hub_id,
            "destination_hub_id": seed_data["hub_dorm"].hub_id,
        },
    )
    uow.commit()

    assert trip.trip_id is not None
    assert trip.status == "PLANNED"

    # Trip retrieval
    fetched = rental_svc.get_trip(trip.trip_id)
    assert fetched.trip_id == trip.trip_id

    # Another student cannot cancel this trip
    with pytest.raises(AppError) as exc:
        rental_svc.cancel_trip(trip.trip_id, seed_data["student2"].user_id)
    assert exc.value.status_code == 403

    # Owner can cancel
    cancelled = rental_svc.cancel_trip(trip.trip_id, seed_data["student"].user_id)
    assert cancelled.status == "CANCELLED"


def test_plan_trip_validation_errors(uow, seed_data):
    rental_svc = RentalService(uow)

    # Missing origin hub
    with pytest.raises(AppError) as exc:
        rental_svc.plan_trip(
            seed_data["student"].user_id,
            {"destination_hub_id": seed_data["hub_dorm"].hub_id},
        )
    assert exc.value.status_code == 400

    # Non-existent hub
    with pytest.raises(NotFoundError):
        rental_svc.plan_trip(
            seed_data["student"].user_id,
            {"origin_hub_id": "non-existent-hub", "destination_hub_id": seed_data["hub_dorm"].hub_id},
        )


def test_cancel_trip_releases_linked_reservation(uow, seed_data, available_public_vehicle):
    rental_svc = RentalService(uow)
    trip = rental_svc.plan_trip(
        seed_data["student"].user_id,
        {
            "origin_hub_id": seed_data["hub_metro"].hub_id,
            "destination_hub_id": seed_data["hub_dorm"].hub_id,
        },
    )
    uow.commit()

    res = rental_svc.reserve_vehicle(
        seed_data["student"].user_id,
        VehicleReservationCreate(vehicle_id=available_public_vehicle.vehicle_id, trip_id=trip.trip_id),
    )
    uow.commit()
    assert available_public_vehicle.status == "RESERVED"

    rental_svc.cancel_trip(trip.trip_id, seed_data["student"].user_id)
    uow.commit()

    assert trip.status == "CANCELLED"
    assert res.status == "CANCELLED"
    assert available_public_vehicle.status == "AVAILABLE"


# --- Search Available Vehicles Tests ---

def test_search_available_vehicles(uow, seed_data, available_public_vehicle):
    rental_svc = RentalService(uow)

    # Add low-battery vehicle (< 20%)
    low_bat = Vehicle(
        vehicle_id="veh-low-bat",
        license_plate="59-R1 10002",
        current_hub_id=seed_data["hub_metro"].hub_id,
        vehicle_type="e-bike",
        ownership_type="PUBLIC",
        battery_pct=15.0,
        status="AVAILABLE",
    )
    uow.vehicles.add(low_bat)

    # Add private vehicle
    private_veh = Vehicle(
        vehicle_id="veh-priv",
        license_plate="59-R1 10003",
        current_hub_id=seed_data["hub_metro"].hub_id,
        vehicle_type="e-bike",
        ownership_type="PRIVATE",
        battery_pct=90.0,
        status="AVAILABLE",
    )
    uow.vehicles.add(private_veh)

    # Add vehicle in MAINTENANCE
    maint_veh = Vehicle(
        vehicle_id="veh-maint",
        license_plate="59-R1 10004",
        current_hub_id=seed_data["hub_metro"].hub_id,
        vehicle_type="e-bike",
        ownership_type="PUBLIC",
        battery_pct=90.0,
        status="MAINTENANCE",
    )
    uow.vehicles.add(maint_veh)
    uow.commit()

    results = rental_svc.search_available_vehicles({"hub_id": seed_data["hub_metro"].hub_id})
    vehicle_ids = [v.vehicle_id for v in results]

    assert available_public_vehicle.vehicle_id in vehicle_ids
    assert "veh-low-bat" not in vehicle_ids  # Below 20%
    assert "veh-priv" not in vehicle_ids  # Private
    assert "veh-maint" not in vehicle_ids  # Maintenance

    # Vehicle type filter
    ebike_results = rental_svc.search_available_vehicles({"vehicle_type": "e-bike"})
    assert available_public_vehicle.vehicle_id in [v.vehicle_id for v in ebike_results]

    escooter_results = rental_svc.search_available_vehicles({"vehicle_type": "e-scooter"})
    assert available_public_vehicle.vehicle_id not in [v.vehicle_id for v in escooter_results]


# --- Reservation Tests ---

def test_reserve_vehicle_and_prevent_double_booking(uow, seed_data, available_public_vehicle):
    rental_svc = RentalService(uow)
    payload = VehicleReservationCreate(vehicle_id=available_public_vehicle.vehicle_id)

    res = rental_svc.reserve_vehicle(seed_data["student"].user_id, payload)
    uow.commit()

    assert res.reservation_id is not None
    assert res.status == "CONFIRMED"
    assert available_public_vehicle.status == "RESERVED"

    # Second user tries to reserve the same vehicle -> double booking rejected (Rule 1)
    payload2 = VehicleReservationCreate(vehicle_id=available_public_vehicle.vehicle_id)
    with pytest.raises(AppError) as exc:
        rental_svc.reserve_vehicle(seed_data["student2"].user_id, payload2)
    assert exc.value.status_code == 409


def test_user_cannot_have_multiple_active_reservations(uow, seed_data, available_public_vehicle):
    rental_svc = RentalService(uow)
    payload = VehicleReservationCreate(vehicle_id=available_public_vehicle.vehicle_id)
    rental_svc.reserve_vehicle(seed_data["student"].user_id, payload)
    uow.commit()

    # Second vehicle
    veh2 = Vehicle(
        vehicle_id="veh-rent-2",
        license_plate="59-R1 20002",
        current_hub_id=seed_data["hub_metro"].hub_id,
        vehicle_type="e-bike",
        ownership_type="PUBLIC",
        battery_pct=80.0,
        status="AVAILABLE",
    )
    uow.vehicles.add(veh2)
    uow.commit()

    # Same student tries to make a second reservation
    with pytest.raises(AppError) as exc:
        rental_svc.reserve_vehicle(seed_data["student"].user_id, VehicleReservationCreate(vehicle_id="veh-rent-2"))
    assert exc.value.status_code == 409


def test_reserve_vehicle_invariants_and_preconditions(uow, seed_data):
    rental_svc = RentalService(uow)

    # 1. Private vehicle cannot be rented
    priv = Vehicle(
        vehicle_id="veh-res-priv",
        license_plate="59-P1 9999",
        ownership_type="PRIVATE",
        status="AVAILABLE",
        battery_pct=90.0,
    )
    uow.vehicles.add(priv)

    # 2. Vehicle with low battery (< 20%)
    low_bat = Vehicle(
        vehicle_id="veh-res-low",
        license_plate="59-P1 8888",
        ownership_type="PUBLIC",
        status="AVAILABLE",
        battery_pct=14.0,
    )
    uow.vehicles.add(low_bat)

    # 3. Vehicle currently in MAINTENANCE
    maint = Vehicle(
        vehicle_id="veh-res-maint",
        license_plate="59-P1 7777",
        ownership_type="PUBLIC",
        status="MAINTENANCE",
        battery_pct=80.0,
    )
    uow.vehicles.add(maint)
    uow.commit()

    # Test Private
    with pytest.raises(AppError) as exc1:
        rental_svc.reserve_vehicle(seed_data["student"].user_id, VehicleReservationCreate(vehicle_id="veh-res-priv"))
    assert exc1.value.status_code == 400

    # Test Low battery
    with pytest.raises(AppError) as exc2:
        rental_svc.reserve_vehicle(seed_data["student"].user_id, VehicleReservationCreate(vehicle_id="veh-res-low"))
    assert exc2.value.status_code == 400

    # Test Maintenance status
    with pytest.raises(AppError) as exc3:
        rental_svc.reserve_vehicle(seed_data["student"].user_id, VehicleReservationCreate(vehicle_id="veh-res-maint"))
    assert exc3.value.status_code == 409


def test_reserve_vehicle_with_unowned_trip_fails(uow, seed_data, available_public_vehicle):
    rental_svc = RentalService(uow)
    # Trip belongs to student2
    trip = rental_svc.plan_trip(
        seed_data["student2"].user_id,
        {"origin_hub_id": seed_data["hub_metro"].hub_id, "destination_hub_id": seed_data["hub_dorm"].hub_id},
    )
    uow.commit()

    # student1 attempts to link student2's trip
    with pytest.raises(AppError) as exc:
        rental_svc.reserve_vehicle(
            seed_data["student"].user_id,
            VehicleReservationCreate(vehicle_id=available_public_vehicle.vehicle_id, trip_id=trip.trip_id),
        )
    assert exc.value.status_code == 403


def test_cancel_reservation(uow, seed_data, available_public_vehicle):
    rental_svc = RentalService(uow)
    res = rental_svc.reserve_vehicle(
        seed_data["student"].user_id,
        VehicleReservationCreate(vehicle_id=available_public_vehicle.vehicle_id),
    )
    uow.commit()

    # Unauthorized cancel
    with pytest.raises(AppError) as exc:
        rental_svc.cancel_reservation(res.reservation_id, seed_data["student2"].user_id)
    assert exc.value.status_code == 403

    # Authorized cancel
    cancelled = rental_svc.cancel_reservation(res.reservation_id, seed_data["student"].user_id)
    assert cancelled.status == "CANCELLED"
    assert available_public_vehicle.status == "AVAILABLE"

    # Cannot cancel already cancelled reservation
    with pytest.raises(AppError) as exc_again:
        rental_svc.cancel_reservation(res.reservation_id, seed_data["student"].user_id)
    assert exc_again.value.status_code == 400


# --- Pickup and Return Lifecycle Tests ---

def test_pickup_and_return_cycle_with_trip_tracking(uow, seed_data, available_public_vehicle):
    rental_svc = RentalService(uow)
    trip = rental_svc.plan_trip(
        seed_data["student"].user_id,
        {"origin_hub_id": seed_data["hub_metro"].hub_id, "destination_hub_id": seed_data["hub_dorm"].hub_id},
    )
    uow.commit()

    res = rental_svc.reserve_vehicle(
        seed_data["student"].user_id,
        VehicleReservationCreate(vehicle_id=available_public_vehicle.vehicle_id, trip_id=trip.trip_id),
    )
    uow.commit()

    # Unauthorized user attempts pickup
    with pytest.raises(AppError) as exc_unauth:
        rental_svc.pick_up(res.reservation_id, seed_data["student2"].user_id)
    assert exc_unauth.value.status_code == 403

    # Authorized pick up
    rental = rental_svc.pick_up(res.reservation_id, seed_data["student"].user_id)
    uow.commit()

    assert rental.rental_id is not None
    assert rental.status == "ACTIVE"
    assert available_public_vehicle.status == "IN_USE"
    assert res.status == "COMPLETED"
    assert trip.status == "IN_PROGRESS"

    # User cannot reserve another vehicle while rental is active
    veh_other = Vehicle(
        vehicle_id="veh-other",
        license_plate="59-R1 88888",
        ownership_type="PUBLIC",
        status="AVAILABLE",
        battery_pct=90.0,
    )
    uow.vehicles.add(veh_other)
    uow.commit()

    with pytest.raises(AppError) as exc_renting:
        rental_svc.reserve_vehicle(seed_data["student"].user_id, VehicleReservationCreate(vehicle_id="veh-other"))
    assert exc_renting.value.status_code == 409

    # Unauthorized user attempts return
    with pytest.raises(AppError) as exc_ret_unauth:
        rental_svc.return_vehicle(rental.rental_id, seed_data["student2"].user_id, seed_data["hub_dorm"].hub_id)
    assert exc_ret_unauth.value.status_code == 403

    # Authorized return at destination hub
    returned_rental = rental_svc.return_vehicle(
        rental.rental_id,
        seed_data["student"].user_id,
        seed_data["hub_dorm"].hub_id,
        condition="GOOD",
    )
    uow.commit()

    assert returned_rental.status == "COMPLETED"
    assert returned_rental.return_hub_id == seed_data["hub_dorm"].hub_id
    assert available_public_vehicle.current_hub_id == seed_data["hub_dorm"].hub_id
    assert available_public_vehicle.status == "AVAILABLE"
    assert trip.status == "COMPLETED"


def test_pickup_past_deadline_expires(uow, seed_data, available_public_vehicle):
    rental_svc = RentalService(uow)
    now = datetime.now(UTC)
    past_start = now - timedelta(minutes=30)
    past_deadline = now - timedelta(minutes=15)

    res = rental_svc.reserve_vehicle(
        seed_data["student"].user_id,
        VehicleReservationCreate(
            vehicle_id=available_public_vehicle.vehicle_id,
            planned_start_at=past_start,
            pickup_deadline_at=past_deadline,
        ),
    )
    uow.commit()

    # Attempt to pick up expired reservation
    with pytest.raises(AppError) as exc:
        rental_svc.pick_up(res.reservation_id, seed_data["student"].user_id)
    assert exc.value.status_code == 400
    assert "expired" in exc.value.message.lower()

    assert res.status == "EXPIRED"
    assert available_public_vehicle.status == "AVAILABLE"


def test_expire_overdue_reservations_selective(uow, seed_data, available_public_vehicle):
    rental_svc = RentalService(uow)
    now = datetime.now(UTC)

    # 1. Overdue reservation (pickup deadline in the past)
    past_deadline = now - timedelta(minutes=10)
    res_overdue = rental_svc.reserve_vehicle(
        seed_data["student"].user_id,
        VehicleReservationCreate(
            vehicle_id=available_public_vehicle.vehicle_id,
            pickup_deadline_at=past_deadline,
        ),
    )
    uow.commit()

    # 2. Active future reservation on a second vehicle
    v2 = Vehicle(
        vehicle_id="v-active-fut",
        license_plate="59-R1 77777",
        ownership_type="PUBLIC",
        status="AVAILABLE",
        battery_pct=90.0,
    )
    uow.vehicles.add(v2)
    uow.commit()

    res_active = rental_svc.reserve_vehicle(
        seed_data["student2"].user_id,
        VehicleReservationCreate(
            vehicle_id="v-active-fut",
            pickup_deadline_at=now + timedelta(minutes=15),
        ),
    )
    uow.commit()

    # Sweep overdue reservations
    expired_list = rental_svc.expire_overdue_reservations()
    uow.commit()

    assert len(expired_list) == 1
    assert expired_list[0].reservation_id == res_overdue.reservation_id
    assert res_overdue.status == "EXPIRED"
    assert available_public_vehicle.status == "AVAILABLE"

    # Future reservation is unaffected
    assert res_active.status == "CONFIRMED"
    assert v2.status == "RESERVED"


def test_return_vehicle_damaged_triggers_maintenance(uow, seed_data, available_public_vehicle):
    rental_svc = RentalService(uow)
    res = rental_svc.reserve_vehicle(
        seed_data["student"].user_id,
        VehicleReservationCreate(vehicle_id=available_public_vehicle.vehicle_id),
    )
    uow.commit()
    rental = rental_svc.pick_up(res.reservation_id, seed_data["student"].user_id)
    uow.commit()

    # Return vehicle with damage report
    rental_svc.return_vehicle(
        rental.rental_id,
        seed_data["student"].user_id,
        seed_data["hub_dorm"].hub_id,
        condition="DAMAGED",
    )
    uow.commit()

    assert available_public_vehicle.status == "MAINTENANCE"
    assert available_public_vehicle.current_hub_id == seed_data["hub_dorm"].hub_id
