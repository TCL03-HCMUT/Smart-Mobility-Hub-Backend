from dataclasses import dataclass
from datetime import datetime, timedelta

from app.schemas.operations import (
    ChargerFault, ChargingDemandSurge, FleetImbalance, ParkingCapacity, RentalDemandSurge, SimulationAdvice,
    SimulationCreate, SimulationMetric,
)

MODEL_VERSION = "demo-2"


@dataclass(frozen=True)
class SimHub:
    hub_id: str
    # vehicle ID and type, in stable order
    vehicles: tuple[tuple[str, str | None], ...]
    acceptable_types: tuple[str | None, ...]
    parking_capacity: int
    parking_free: int
    parking_bookable: int


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
    charging_duration_minutes: int


@dataclass(frozen=True)
class SimulationResult:
    metrics: list[SimulationMetric]
    recommendations: list[SimulationAdvice]


class DemandSimulationEngine:
    """Project two branches from immutable, database-independent inputs."""

    def run(self, snapshot: OperationalSnapshot, scenario: SimulationCreate) -> SimulationResult:
        if isinstance(scenario, RentalDemandSurge):
            return self._rentals(snapshot, scenario)
        if isinstance(scenario, ParkingCapacity):
            return self._parking(snapshot, scenario)
        if isinstance(scenario, FleetImbalance):
            return self._fleet(snapshot, scenario)
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
        advice = self._incoming_dispatch(snapshot, target, projection[2])
        return SimulationResult(metrics, advice)

    def _incoming_dispatch(self, snapshot, target, unmet):
        if not unmet:
            return []
        donors = sorted(
            (hub for hub in snapshot.hubs if hub.hub_id != target.hub_id and len(hub.vehicles) > 1),
            key=lambda hub: (-len(hub.vehicles), hub.hub_id),
        )
        for donor in donors:
            advice = self._dispatch(donor, target, f"Projected unmet demand is {unmet} requests.")
            if advice:
                return [advice]
        return [SimulationAdvice(
            type="MONITOR_DEMAND",
            reason=f"Projected unmet demand is {unmet} requests. "
            "The snapshot has no eligible donor vehicle and destination space.",
        )]

    @staticmethod
    def _dispatch(source, target, reason):
        for vehicle_id, vehicle_type in source.vehicles:
            if vehicle_type in target.acceptable_types:
                return SimulationAdvice(
                    type="REDISTRIBUTE_VEHICLE", vehicle_id=vehicle_id,
                    from_hub_id=source.hub_id, to_hub_id=target.hub_id,
                    reason=f"{reason} The snapshot shows an eligible vehicle at {source.hub_id} "
                    f"and a compatible parking space at {target.hub_id}. "
                    "Confirm through the dispatch workflow, which checks live capacity.",
                )
        return None

    def _parking(self, snapshot, scenario):
        target = next(hub for hub in snapshot.hubs if hub.hub_id == scenario.hub_id)
        admitted = min(target.parking_bookable, scenario.demand_count)
        occupied = target.parking_capacity - target.parking_free
        rejected = scenario.demand_count - admitted
        remaining = target.parking_bookable - admitted
        metrics = compare(
            ("parking_arrivals", "parking_occupied_at_end", "parking_bookable_at_end", "parking_rejected"),
            ("vehicles", "spaces", "spaces", "vehicles"),
            (0, occupied, target.parking_bookable, 0),
            (scenario.demand_count, occupied + admitted, remaining, rejected),
        )
        advice = []
        if remaining == 0:
            alternative = max(
                (hub for hub in snapshot.hubs if hub.hub_id != target.hub_id and hub.parking_bookable),
                key=lambda hub: hub.parking_bookable, default=None,
            )
            reason = f"Projected bookable parking is full at {target.hub_id}. "
            reason += (
                f"Check live availability at {alternative.hub_id} before redirecting arrivals."
                if alternative else "Monitor availability across Hubs before accepting additional arrivals."
            )
            advice.append(SimulationAdvice(type="REDIRECT_PARKING", reason=reason))
        return SimulationResult(metrics, advice)

    def _fleet(self, snapshot, scenario):
        target = next(hub for hub in snapshot.hubs if hub.hub_id == scenario.hub_id)
        count = len(target.vehicles)
        demand = snapshot.baseline_rental_count
        projected = (
            max(0, count - scenario.vehicle_count) if scenario.direction == "SHORTAGE"
            else count + scenario.vehicle_count
        )
        unmet = max(0, demand - projected)
        surplus = max(0, projected - demand)
        metrics = compare(
            ("available_vehicles_at_start", "unmet_rental_demand", "available_vehicles_at_end"),
            ("vehicles", "requests", "vehicles"),
            (count, max(0, demand - count), max(0, count - demand)),
            (projected, unmet, surplus),
        )
        if scenario.direction == "SHORTAGE":
            return SimulationResult(metrics, self._incoming_dispatch(snapshot, target, unmet))

        advice = []
        if surplus:
            recipients = sorted(
                (hub for hub in snapshot.hubs if hub != target and len(hub.vehicles) < demand),
                key=lambda hub: (len(hub.vehicles), hub.hub_id),
            )
            if count > demand:
                for recipient in recipients:
                    dispatch = self._dispatch(target, recipient, f"Projected surplus is {surplus} vehicles.")
                    if dispatch:
                        return SimulationResult(metrics, [dispatch])
            advice.append(SimulationAdvice(
                type="MONITOR_FLEET",
                reason=f"Projected surplus is {surplus} vehicles at {target.hub_id}. "
                "Review live fleet levels and compatible parking at Hubs with a shortage before moving vehicles.",
            ))
        return SimulationResult(metrics, advice)

    def _charging(self, snapshot, scenario: ChargerFault | ChargingDemandSurge):
        start = snapshot.captured_at
        horizon = start + timedelta(minutes=scenario.horizon_minutes)
        fault = scenario.charging_point_id if isinstance(scenario, ChargerFault) else None
        additional = ()
        if isinstance(scenario, ChargingDemandSurge):
            additional = tuple(
                SimCharge(
                    request_id=f"projected-{index}", duration=snapshot.charging_duration_minutes,
                    deadline=horizon, need_by=None, battery=None, requested_at=start,
                    point_id=None, starts_at=None, finishes_at=None, active=False,
                )
                for index in range(scenario.demand_count)
            )

        def replay(faulted_point, extra=()):
            points = set(snapshot.charging_points) - {faulted_point}
            queue = list(snapshot.charging_requests + extra)
            requests = len(queue)
            # point -> (request, end); assignments in the copied state can change independently.
            running = {}
            finished = 0
            capacity = min(snapshot.charging_capacity, len(points))
            for job in tuple(queue):
                if job.active and job.point_id in points and job.finishes_at:
                    running[job.point_id] = (job, job.finishes_at)
                    queue.remove(job)
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
            return capacity, finished, requests - finished

        baseline = replay(None)
        projection = replay(fault, additional)
        metrics = compare(
            ("charging_capacity", "charges_completed", "charges_not_completed_at_horizon"),
            ("points", "requests", "requests"), baseline, projection,
        )
        if fault:
            advice = [SimulationAdvice(
                type="RESTORE_CHARGER",
                reason=f"Repair {fault} or reschedule affected parked vehicles. "
                f"Usable capacity changes from {baseline[0]} to {projection[0]} and "
                f"{projection[2]} requests remain incomplete at the horizon.",
            )]
        else:
            metrics += compare(
                ("charging_requests",), ("requests",),
                (len(snapshot.charging_requests),), (len(snapshot.charging_requests) + len(additional),),
            )
            advice = [SimulationAdvice(
                type="RESCHEDULE_CHARGING",
                reason=f"{projection[2]} requests remain incomplete at the horizon. "
                "Review later charging slots or working charging points at another Hub.",
            )] if projection[2] else []
        return SimulationResult(metrics, advice)


def compare(names, units, baseline, scenario):
    return [
        SimulationMetric(name=name, unit=unit, baseline=before, scenario=after)
        for name, unit, before, after in zip(names, units, baseline, scenario, strict=True)
    ]
