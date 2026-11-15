from dataclasses import dataclass
from datetime import datetime, timedelta

from app.schemas.operations import ChargerFault, RentalDemandSurge, SimulationAdvice, SimulationMetric

MODEL_VERSION = "demo-1"


@dataclass(frozen=True)
class SimHub:
    hub_id: str
    # vehicle ID and type, in stable order
    vehicles: tuple[tuple[str, str | None], ...]
    acceptable_types: tuple[str | None, ...]


@dataclass(frozen=True)
class SimCharge:
    request_id: str
    duration: int
    deadline: datetime
    need_by: datetime | None
    battery: float | None
    requested_at: datetime
    point_id: str | None
    starts_at: datetime | None
    finishes_at: datetime | None
    active: bool


@dataclass(frozen=True)
class OperationalSnapshot:
    revision: str
    captured_at: datetime
    hubs: tuple[SimHub, ...]
    charging_points: tuple[str, ...]
    charging_capacity: int
    charging_requests: tuple[SimCharge, ...]
    step_minutes: int
    baseline_rental_count: int


@dataclass(frozen=True)
class SimulationResult:
    metrics: list[SimulationMetric]
    recommendations: list[SimulationAdvice]


class DemandSimulationEngine:
    """Project two branches from immutable, database-independent inputs."""

    def run(self, snapshot: OperationalSnapshot, scenario: RentalDemandSurge | ChargerFault) -> SimulationResult:
        if isinstance(scenario, RentalDemandSurge):
            return self._rentals(snapshot, scenario)
        return self._charging(snapshot, scenario)

    def _rentals(self, snapshot, scenario):
        target = next(hub for hub in snapshot.hubs if hub.hub_id == scenario.hub_id)
        starts = range(0, scenario.horizon_minutes, snapshot.step_minutes)
        steps = len(starts)

        def replay(demand):
            vehicles = list(target.vehicles)
            served = 0
            for index in range(steps):
                # Spread whole arrivals over all time steps, keeping the same total.
                arrivals = ((index + 1) * demand) // steps - (index * demand) // steps
                allocated = min(len(vehicles), arrivals)
                del vehicles[:allocated]
                served += allocated
            return demand, served, demand - served, len(vehicles)

        baseline_demand = snapshot.baseline_rental_count
        baseline = replay(baseline_demand)
        projection = replay(baseline_demand + scenario.demand_count)
        metrics = compare(
            ("rental_requests", "rentals_served", "unmet_rental_demand", "available_vehicles_at_end"),
            ("requests", "requests", "requests", "vehicles"), baseline, projection,
        )
        advice = []
        if projection[2]:
            donors = sorted(
                (hub for hub in snapshot.hubs if hub.hub_id != target.hub_id and len(hub.vehicles) > 1),
                key=lambda hub: (-len(hub.vehicles), hub.hub_id),
            )
            candidate = next((
                (hub, vehicle_id) for hub in donors for vehicle_id, vehicle_type in hub.vehicles
                if vehicle_type in target.acceptable_types
            ), None)
            if candidate:
                source, vehicle_id = candidate
                advice.append(SimulationAdvice(
                    type="REDISTRIBUTE_VEHICLE", vehicle_id=vehicle_id,
                    from_hub_id=source.hub_id, to_hub_id=target.hub_id,
                    reason=f"Projected unmet demand is {projection[2]} requests. "
                    f"{source.hub_id} has spare vehicles and the snapshot shows a compatible destination space. "
                    "Confirm through the dispatch workflow, which checks live capacity.",
                ))
            else:
                advice.append(SimulationAdvice(
                    type="MONITOR_DEMAND",
                    reason=f"Projected unmet demand is {projection[2]} requests. "
                    "The snapshot has no eligible donor vehicle and destination space.",
                ))
        return SimulationResult(metrics, advice)

    def _charging(self, snapshot, scenario):
        def replay(faulted_point):
            points = set(snapshot.charging_points) - {faulted_point}
            queue = list(snapshot.charging_requests)
            # point -> (request, end); assignments in the copied state can change independently.
            running = {}
            finished = 0
            capacity = min(snapshot.charging_capacity, len(points))
            for job in tuple(queue):
                if job.active and job.point_id in points and job.finishes_at:
                    running[job.point_id] = (job, job.finishes_at)
                    queue.remove(job)
            start = snapshot.captured_at
            horizon = start + timedelta(minutes=scenario.horizon_minutes)
            for minute in (*range(0, scenario.horizon_minutes, snapshot.step_minutes), scenario.horizon_minutes):
                now = start + timedelta(minutes=minute)
                for point, (job, end) in tuple(running.items()):
                    if end <= now:
                        finished += int(end <= job.deadline)
                        del running[point]
                if now >= horizon:
                    break
                # Prefer held schedules, otherwise mirror the live priority rule.
                # Stable sorting keeps the persistent submission order for equal timestamps.
                queue.sort(key=lambda job: (
                    not (job.point_id in points and job.starts_at is not None and not job.active),
                    job.need_by or datetime.max.replace(tzinfo=now.tzinfo),
                    job.battery if job.battery is not None else float("inf"),
                    job.requested_at,
                ))
                for job in tuple(queue):
                    if len(running) >= capacity:
                        break
                    if job.point_id in points and job.starts_at and now < job.starts_at and not job.active:
                        continue
                    end = now + timedelta(minutes=job.duration)
                    if end > job.deadline:
                        continue
                    reserved_for_later = {
                        other.point_id for other in queue
                        if other != job and not other.active and other.starts_at and now < other.starts_at
                    }
                    free = points - running.keys() - reserved_for_later
                    if not free:
                        continue
                    point = job.point_id if job.point_id in free else min(free)
                    running[point] = (job, end)
                    queue.remove(job)
            return capacity, finished, len(snapshot.charging_requests) - finished

        baseline = replay(None)
        projection = replay(scenario.charging_point_id)
        metrics = compare(
            ("charging_capacity", "charges_completed", "charges_not_completed_at_horizon"),
            ("points", "requests", "requests"), baseline, projection,
        )
        advice = [SimulationAdvice(
            type="RESTORE_CHARGER",
            reason=f"Repair {scenario.charging_point_id} or reschedule affected parked vehicles. "
            f"Usable capacity changes from {baseline[0]} to {projection[0]} and "
            f"{projection[2]} requests remain incomplete at the horizon.",
        )]
        return SimulationResult(metrics, advice)


def compare(names, units, baseline, scenario):
    return [
        SimulationMetric(name=name, unit=unit, baseline=before, scenario=after)
        for name, unit, before, after in zip(names, units, baseline, scenario, strict=True)
    ]
