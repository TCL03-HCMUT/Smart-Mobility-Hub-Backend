from app.core.exceptions import AppError
from app.core.utils import utc_now_iso, uuid_id
from app.models.entities import PrivateVehicle, PublicVehicle, SensorTelemetry, Vehicle
from app.schemas.fleet import PrivateVehicleCreate, TelemetryCreate, VehicleStatusUpdate
from app.services.base import BaseService

VALID_VEHICLE_STATUSES = {
    "AVAILABLE",
    "RESERVED",
    "IN_USE",
    "MAINTENANCE",
    "IN_TRANSIT",
    "CHARGING",
}


class FleetService(BaseService):
    """Business logic for fleet management, asset tracking, and private EV registration."""

    def register_private_vehicle(self, owner_id: str, payload: PrivateVehicleCreate) -> Vehicle:
        """Register a privately owned electric vehicle for a student or commuter."""
        owner = self.uow.users.get_by_id(owner_id)
        self._ensure(owner is not None, f"User {owner_id} was not found", status_code=404)

        existing = self.uow.vehicles.find_by_license_plate(payload.license_plate)
        if existing:
            raise AppError(
                f"Vehicle with license plate '{payload.license_plate}' already exists",
                status_code=409,
            )

        if payload.current_hub_id:
            self._get_or_404(self.uow.hubs, payload.current_hub_id, "MobilityHub")

        if payload.battery_pct is not None:
            self._ensure(
                0.0 <= payload.battery_pct <= 100.0,
                "Battery percentage must be between 0.0 and 100.0",
            )

        vehicle_id = uuid_id()
        vehicle = Vehicle(
            vehicle_id=vehicle_id,
            license_plate=payload.license_plate,
            current_hub_id=payload.current_hub_id,
            vehicle_type=payload.vehicle_type or "e-scooter",
            ownership_type="PRIVATE",
            battery_pct=payload.battery_pct if payload.battery_pct is not None else 100.0,
            status="AVAILABLE",
        )
        self.uow.vehicles.add(vehicle)
        self.uow.session.flush()

        private_vehicle = PrivateVehicle(
            vehicle_id=vehicle_id,
            owner_id=owner_id,
            rfid_tag_code=payload.rfid_tag_code,
        )
        self.uow.private_vehicles.add(private_vehicle)
        self.uow.session.flush()
        return vehicle

    def get_vehicle(self, vehicle_id: str) -> Vehicle:
        """Retrieve vehicle details by ID."""
        return self._get_or_404(self.uow.vehicles, vehicle_id, "Vehicle")

    def search_vehicles(self, criteria: dict) -> list[Vehicle]:
        """Search and filter vehicles based on criteria."""
        return self.uow.vehicles.search(criteria)

    def update_status(self, vehicle_id: str, payload: VehicleStatusUpdate) -> Vehicle:
        """Update vehicle status, location, and battery level following transition invariants."""
        vehicle = self._get_or_404(self.uow.vehicles, vehicle_id, "Vehicle")

        self._ensure(
            payload.status in VALID_VEHICLE_STATUSES,
            f"Invalid vehicle status '{payload.status}'. Allowed: {sorted(VALID_VEHICLE_STATUSES)}",
        )

        if vehicle.status == "MAINTENANCE" and payload.status in ("IN_USE", "RESERVED"):
            raise AppError(
                "A vehicle in MAINTENANCE must transition to AVAILABLE before being reserved or used",
                status_code=400,
            )

        if payload.current_hub_id is not None:
            if payload.current_hub_id:
                self._get_or_404(self.uow.hubs, payload.current_hub_id, "MobilityHub")
            vehicle.current_hub_id = payload.current_hub_id

        if payload.battery_pct is not None:
            self._ensure(
                0.0 <= payload.battery_pct <= 100.0,
                "Battery percentage must be between 0.0 and 100.0",
            )
            vehicle.battery_pct = payload.battery_pct

        vehicle.status = payload.status
        self.uow.session.flush()
        return vehicle

    def record_telemetry(self, payload: TelemetryCreate) -> SensorTelemetry:
        """Record live sensor telemetry adhering to the XOR constraint between vehicle and resource."""
        has_vehicle = payload.vehicle_id is not None
        has_resource = payload.resource_id is not None
        if not (has_vehicle ^ has_resource):
            raise AppError(
                "Telemetry must specify either vehicle_id or resource_id, not both or neither",
                status_code=400,
            )

        if payload.vehicle_id:
            vehicle = self._get_or_404(self.uow.vehicles, payload.vehicle_id, "Vehicle")
            if payload.metric_name == "battery_pct" and payload.metric_value is not None:
                self._ensure(
                    0.0 <= payload.metric_value <= 100.0,
                    "Battery percentage must be between 0.0 and 100.0",
                )
                vehicle.battery_pct = payload.metric_value

        if payload.resource_id:
            self._get_or_404(self.uow.resources, payload.resource_id, "HubResource")

        telemetry = SensorTelemetry(
            sensor_id=uuid_id(),
            vehicle_id=payload.vehicle_id,
            resource_id=payload.resource_id,
            metric_name=payload.metric_name,
            metric_value=payload.metric_value,
            is_simulated=1 if payload.is_simulated else 0,
            timestamp=utc_now_iso(),
        )
        self.uow.telemetry.add(telemetry)
        self.uow.session.flush()
        return telemetry

    def process_telemetry(self, telemetry_id: str) -> dict:
        """Analyze recorded telemetry data for operational thresholds and anomalies."""
        telemetry = self._get_or_404(self.uow.telemetry, telemetry_id, "SensorTelemetry")

        alerts: list[str] = []
        if telemetry.metric_name == "battery_pct" and telemetry.metric_value is not None:
            if telemetry.metric_value < 15.0:
                alerts.append("CRITICAL_BATTERY_LOW")
            elif telemetry.metric_value < 20.0:
                alerts.append("BATTERY_LOW_UNAVAILABLE_FOR_RENTAL")
        elif telemetry.metric_name == "temperature" and telemetry.metric_value is not None:
            if telemetry.metric_value > 60.0:
                alerts.append("OVERHEATING_WARNING")

        return {
            "telemetry_id": telemetry.sensor_id,
            "vehicle_id": telemetry.vehicle_id,
            "resource_id": telemetry.resource_id,
            "metric_name": telemetry.metric_name,
            "metric_value": telemetry.metric_value,
            "timestamp": telemetry.timestamp,
            "status": "PROCESSED",
            "alerts": alerts,
        }

    def schedule_maintenance(self, vehicle_id: str, scheduled_at: str) -> PublicVehicle:
        """Schedule routine maintenance for a public fleet vehicle."""
        vehicle = self._get_or_404(self.uow.vehicles, vehicle_id, "Vehicle")
        public_vehicle = self.uow.public_vehicles.get_by_id(vehicle_id)
        self._ensure(
            public_vehicle is not None,
            f"Vehicle {vehicle_id} is not a public fleet vehicle",
            status_code=400,
        )

        public_vehicle.maintenance_schedule = scheduled_at
        vehicle.status = "MAINTENANCE"
        self.uow.session.flush()
        return public_vehicle

    def mark_in_transit(self, vehicle_id: str, from_hub_id: str, to_hub_id: str) -> Vehicle:
        """Mark a vehicle as moving between hubs during redistribution."""
        vehicle = self._get_or_404(self.uow.vehicles, vehicle_id, "Vehicle")
        self._get_or_404(self.uow.hubs, from_hub_id, "MobilityHub")
        self._get_or_404(self.uow.hubs, to_hub_id, "MobilityHub")

        self._ensure(
            vehicle.current_hub_id == from_hub_id,
            f"Vehicle is currently at hub '{vehicle.current_hub_id}', not origin hub '{from_hub_id}'",
            status_code=400,
        )
        self._ensure(
            vehicle.status in ("AVAILABLE", "MAINTENANCE"),
            f"Vehicle in status '{vehicle.status}' cannot be moved in transit",
            status_code=400,
        )

        vehicle.status = "IN_TRANSIT"
        vehicle.current_hub_id = None
        self.uow.session.flush()
        return vehicle
