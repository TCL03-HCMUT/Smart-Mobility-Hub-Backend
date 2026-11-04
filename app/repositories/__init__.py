from app.repositories.base import BaseRepository
from app.repositories.contracts import Repository
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
from app.repositories.unit_of_work import UnitOfWork

__all__ = [
    "BaseRepository",
    "Repository",
    "UnitOfWork",
    "VehicleRepository",
    "PrivateVehicleRepository",
    "PublicVehicleRepository",
    "SensorTelemetryRepository",
    "RentalRepository",
    "TripRepository",
    "VehicleReservationRepository",
]

