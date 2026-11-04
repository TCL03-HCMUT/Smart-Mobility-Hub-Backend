from app.models.entities import (
    ChargingPoint,
    ChargingSession,
    CoordinationRecommendation,
    HubResource,
    MobilityHub,
    OperationalIncident,
    ParkingReservation,
    ParkingSession,
    ParkingSpace,
    RedistributionTask,
    Role,
    SimulationRun,
    StudentProfile,
    User,
    UserRole,
)
from app.repositories.base import BaseRepository
from app.repositories.fleet import (
    PrivateVehicleRepository,
    PublicVehicleRepository,
    SensorTelemetryRepository,
    VehicleRepository,
)
from app.repositories.rental import (
    RentalRepository,
    TripRepository,
    VehicleReservationRepository,
)


class MobilityHubRepository(BaseRepository[MobilityHub]):
    model = MobilityHub


class HubResourceRepository(BaseRepository[HubResource]):
    model = HubResource


class ParkingReservationRepository(BaseRepository[ParkingReservation]):
    model = ParkingReservation


class ParkingSessionRepository(BaseRepository[ParkingSession]):
    model = ParkingSession


class ParkingSpaceRepository(BaseRepository[ParkingSpace]):
    model = ParkingSpace


class ChargingSessionRepository(BaseRepository[ChargingSession]):
    model = ChargingSession


class ChargingPointRepository(BaseRepository[ChargingPoint]):
    model = ChargingPoint


class OperationalIncidentRepository(BaseRepository[OperationalIncident]):
    model = OperationalIncident


class SimulationRunRepository(BaseRepository[SimulationRun]):
    model = SimulationRun


class CoordinationRecommendationRepository(BaseRepository[CoordinationRecommendation]):
    model = CoordinationRecommendation


class RedistributionTaskRepository(BaseRepository[RedistributionTask]):
    model = RedistributionTask


class UserRepository(BaseRepository[User]):
    model = User


class RoleRepository(BaseRepository[Role]):
    model = Role


class UserRoleRepository(BaseRepository[UserRole]):
    model = UserRole


class StudentProfileRepository(BaseRepository[StudentProfile]):
    model = StudentProfile


__all__ = [
    "VehicleRepository",
    "PrivateVehicleRepository",
    "PublicVehicleRepository",
    "SensorTelemetryRepository",
    "RentalRepository",
    "TripRepository",
    "VehicleReservationRepository",
    "MobilityHubRepository",
    "HubResourceRepository",
    "ParkingReservationRepository",
    "ParkingSessionRepository",
    "ParkingSpaceRepository",
    "ChargingSessionRepository",
    "ChargingPointRepository",
    "OperationalIncidentRepository",
    "SimulationRunRepository",
    "CoordinationRecommendationRepository",
    "RedistributionTaskRepository",
    "UserRepository",
    "RoleRepository",
    "UserRoleRepository",
    "StudentProfileRepository",
]
