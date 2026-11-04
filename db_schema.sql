-- 1. SHARED KERNEL (IAM & BASE DOMAIN)
CREATE TABLE "USER" (
    user_id TEXT PRIMARY KEY, -- UUID
    sso_subject TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE,
    display_name TEXT,
    user_type TEXT CHECK(user_type IN ('STUDENT', 'OPERATOR')),
    status TEXT
);

CREATE TABLE ROLE (
    role_id TEXT PRIMARY KEY, -- UUID
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE USER_ROLE (
    user_id TEXT NOT NULL,
    role_id TEXT NOT NULL,
    PRIMARY KEY (user_id, role_id),
    FOREIGN KEY (user_id) REFERENCES "USER"(user_id) ON DELETE CASCADE,
    FOREIGN KEY (role_id) REFERENCES ROLE(role_id) ON DELETE CASCADE
);

CREATE TABLE MOBILITY_HUB (
    hub_id TEXT PRIMARY KEY, -- UUID
    name TEXT NOT NULL,
    hub_type TEXT,
    latitude TEXT,
    longitude TEXT,
    max_parking_capacity INTEGER DEFAULT 0,
    max_charging_capacity INTEGER DEFAULT 0,
    status TEXT
);

CREATE TABLE HUB_RESOURCE (
    resource_id TEXT PRIMARY KEY, -- UUID
    hub_id TEXT NOT NULL,
    code TEXT NOT NULL,
    resource_type TEXT CHECK(resource_type IN ('PARKING_SPACE', 'CHARGING_POINT')),
    FOREIGN KEY (hub_id) REFERENCES MOBILITY_HUB(hub_id)
);

-- 2. RENTAL MODULE
CREATE TABLE STUDENT_PROFILE (
    user_id TEXT PRIMARY KEY, -- FK is also PK (1:1)
    student_code TEXT UNIQUE NOT NULL,
    university_name TEXT,
    faculty TEXT,
    FOREIGN KEY (user_id) REFERENCES "USER"(user_id) ON DELETE CASCADE
);

CREATE TABLE TRIP (
    trip_id TEXT PRIMARY KEY, -- UUID
    student_id TEXT NOT NULL,
    origin_hub_id TEXT NOT NULL,
    destination_hub_id TEXT NOT NULL,
    planned_departure TEXT, -- TIMESTAMP
    status TEXT CHECK(status IN ('PLANNED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED')),
    FOREIGN KEY (student_id) REFERENCES "USER"(user_id),
    FOREIGN KEY (origin_hub_id) REFERENCES MOBILITY_HUB(hub_id),
    FOREIGN KEY (destination_hub_id) REFERENCES MOBILITY_HUB(hub_id)
);

CREATE TABLE VEHICLE_RESERVATION (
    reservation_id TEXT PRIMARY KEY, -- UUID
    trip_id TEXT, -- NULLABLE
    student_id TEXT NOT NULL,
    vehicle_id TEXT NOT NULL,
    reserved_at TEXT NOT NULL, -- TIMESTAMP
    planned_start_at TEXT, -- TIMESTAMP
    pickup_deadline_at TEXT, -- TIMESTAMP
    status TEXT CHECK(status IN ('CONFIRMED', 'PENDING', 'RESERVED', 'COMPLETED', 'CANCELLED', 'EXPIRED')),
    FOREIGN KEY (trip_id) REFERENCES TRIP(trip_id),
    FOREIGN KEY (student_id) REFERENCES "USER"(user_id),
    FOREIGN KEY (vehicle_id) REFERENCES VEHICLE(vehicle_id)
);

CREATE TABLE RENTAL (
    rental_id TEXT PRIMARY KEY, -- UUID
    reservation_id TEXT UNIQUE NOT NULL, -- 1:0..1 constraint enforced here
    origin_hub_id TEXT NOT NULL,
    return_hub_id TEXT, -- NULLABLE (until returned)
    picked_up_at TEXT, -- TIMESTAMP
    returned_at TEXT, -- TIMESTAMP
    return_condition TEXT,
    status TEXT CHECK(status IN ('ACTIVE', 'COMPLETED', 'DISPUTED')),
    FOREIGN KEY (reservation_id) REFERENCES VEHICLE_RESERVATION(reservation_id),
    FOREIGN KEY (origin_hub_id) REFERENCES MOBILITY_HUB(hub_id),
    FOREIGN KEY (return_hub_id) REFERENCES MOBILITY_HUB(hub_id)
);

-- 3. PARKING & SPACE MODULE
CREATE TABLE PARKING_SPACE (
    resource_id TEXT PRIMARY KEY, -- FK is also PK
    supported_vehicle_type TEXT,
    FOREIGN KEY (resource_id) REFERENCES HUB_RESOURCE(resource_id) ON DELETE CASCADE
);

CREATE TABLE PARKING_RESERVATION (
    reservation_id TEXT PRIMARY KEY, -- UUID
    user_id TEXT NOT NULL,
    vehicle_id TEXT NOT NULL,
    parking_space_id TEXT NOT NULL,
    starts_at TEXT, -- TIMESTAMP
    ends_at TEXT, -- TIMESTAMP
    status TEXT CHECK(status IN ('RESERVED', 'CHECKED_IN', 'COMPLETED', 'CANCELLED', 'NOSHOW', 'OVERDUE')),
    FOREIGN KEY (user_id) REFERENCES "USER"(user_id),
    FOREIGN KEY (vehicle_id) REFERENCES VEHICLE(vehicle_id),
    FOREIGN KEY (parking_space_id) REFERENCES PARKING_SPACE(resource_id)
);

CREATE TABLE PARKING_SESSION (
    session_id TEXT PRIMARY KEY, -- UUID
    reservation_id TEXT, -- NULLABLE (supports walk-ins)
    user_id TEXT NOT NULL,
    vehicle_id TEXT NOT NULL,
    parking_space_id TEXT NOT NULL,
    checked_in_at TEXT, -- TIMESTAMP
    checked_out_at TEXT, -- TIMESTAMP
    FOREIGN KEY (reservation_id) REFERENCES PARKING_RESERVATION(reservation_id),
    FOREIGN KEY (user_id) REFERENCES "USER"(user_id),
    FOREIGN KEY (vehicle_id) REFERENCES VEHICLE(vehicle_id),
    FOREIGN KEY (parking_space_id) REFERENCES PARKING_SPACE(resource_id)
);

-- 4. CHARGING MODULE
CREATE TABLE CHARGING_POINT (
    resource_id TEXT PRIMARY KEY, -- FK is also PK
    connector_type TEXT,
    max_power_kw REAL, -- DECIMAL mapping
    FOREIGN KEY (resource_id) REFERENCES HUB_RESOURCE(resource_id) ON DELETE CASCADE
);

CREATE TABLE CHARGING_SESSION (
    session_id TEXT PRIMARY KEY, -- UUID
    user_id TEXT NOT NULL,
    vehicle_id TEXT NOT NULL,
    charging_point_id TEXT NOT NULL,
    target_battery_pct REAL,
    target_duration_minutes INTEGER,
    priority_score INTEGER,
    scheduled_start_time TEXT, -- TIMESTAMP
    actual_start_time TEXT, -- TIMESTAMP
    status TEXT CHECK(status IN ('SUBMITTED', 'QUEUED', 'CHARGING', 'COMPLETED', 'CANCELLED', 'INTERRUPTED', 'FAILED')),
    FOREIGN KEY (user_id) REFERENCES "USER"(user_id),
    FOREIGN KEY (vehicle_id) REFERENCES VEHICLE(vehicle_id),
    FOREIGN KEY (charging_point_id) REFERENCES CHARGING_POINT(resource_id)
);

CREATE TABLE CHARGING_POLICY (
    policy_id TEXT PRIMARY KEY, -- UUID
    hub_id TEXT NOT NULL,
    updated_by_user_id TEXT NOT NULL,
    priority_weights TEXT, -- JSON mapping
    low_battery_threshold_pct REAL,
    is_active INTEGER, -- BOOLEAN (0/1)
    FOREIGN KEY (hub_id) REFERENCES MOBILITY_HUB(hub_id),
    FOREIGN KEY (updated_by_user_id) REFERENCES "USER"(user_id)
);

-- 5. FLEET MODULE
CREATE TABLE VEHICLE (
    vehicle_id TEXT PRIMARY KEY, -- UUID
    license_plate TEXT UNIQUE NOT NULL,
    current_hub_id TEXT, -- NULLABLE (might be in transit)
    vehicle_type TEXT,
    ownership_type TEXT CHECK(ownership_type IN ('PUBLIC', 'PRIVATE')),
    battery_pct REAL CHECK(battery_pct >= 0.0 AND battery_pct <= 100.0),
    status TEXT CHECK(status IN ('AVAILABLE', 'RESERVED', 'IN_USE', 'MAINTENANCE', 'IN_TRANSIT', 'CHARGING')),
    FOREIGN KEY (current_hub_id) REFERENCES MOBILITY_HUB(hub_id)
);

CREATE TABLE PUBLIC_VEHICLE (
    vehicle_id TEXT PRIMARY KEY, -- FK is also PK
    fleet_batch_code TEXT,
    maintenance_schedule TEXT, -- TIMESTAMP
    FOREIGN KEY (vehicle_id) REFERENCES VEHICLE(vehicle_id) ON DELETE CASCADE
);

CREATE TABLE PRIVATE_VEHICLE (
    vehicle_id TEXT PRIMARY KEY, -- FK is also PK
    owner_id TEXT NOT NULL,
    rfid_tag_code TEXT,
    FOREIGN KEY (vehicle_id) REFERENCES VEHICLE(vehicle_id) ON DELETE CASCADE,
    FOREIGN KEY (owner_id) REFERENCES "USER"(user_id)
);

CREATE TABLE SENSOR_TELEMETRY (
    sensor_id TEXT PRIMARY KEY, -- UUID
    vehicle_id TEXT,
    resource_id TEXT,
    metric_name TEXT NOT NULL,
    metric_value REAL,
    is_simulated INTEGER, -- BOOLEAN
    timestamp TEXT NOT NULL, -- TIMESTAMP
    FOREIGN KEY (vehicle_id) REFERENCES VEHICLE(vehicle_id),
    FOREIGN KEY (resource_id) REFERENCES HUB_RESOURCE(resource_id),
    -- XOR CONSTRAINT: Must have one, but never both, never neither.
    CONSTRAINT chk_telemetry_fks CHECK (
        (vehicle_id IS NOT NULL AND resource_id IS NULL) OR 
        (vehicle_id IS NULL AND resource_id IS NOT NULL)
    )
);

-- 6. OPERATIONS MODULE
CREATE TABLE OPERATIONAL_INCIDENT (
    incident_id TEXT PRIMARY KEY, -- UUID
    hub_id TEXT NOT NULL,
    vehicle_id TEXT,
    resource_id TEXT,
    reported_by_user_id TEXT NOT NULL,
    resolved_by_user_id TEXT, -- NULLABLE until resolved
    incident_type TEXT,
    severity TEXT,
    description TEXT,
    status TEXT CHECK(status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED', 'CLOSED')),
    opened_at TEXT NOT NULL, -- TIMESTAMP
    closed_at TEXT, -- TIMESTAMP
    FOREIGN KEY (hub_id) REFERENCES MOBILITY_HUB(hub_id),
    FOREIGN KEY (vehicle_id) REFERENCES VEHICLE(vehicle_id),
    FOREIGN KEY (resource_id) REFERENCES HUB_RESOURCE(resource_id),
    FOREIGN KEY (reported_by_user_id) REFERENCES "USER"(user_id),
    FOREIGN KEY (resolved_by_user_id) REFERENCES "USER"(user_id),
    -- CONSTRAINT: An incident must involve an asset, but both cannot be null.
    CONSTRAINT chk_incident_assets CHECK (
        vehicle_id IS NOT NULL OR resource_id IS NOT NULL
    )
);

CREATE TABLE REDISTRIBUTION_TASK (
    task_id TEXT PRIMARY KEY, -- UUID
    recommendation_id TEXT, -- NULLABLE (Allows manual task creation)
    vehicle_id TEXT NOT NULL,
    from_hub_id TEXT NOT NULL,
    to_hub_id TEXT NOT NULL,
    created_by_user_id TEXT NOT NULL,
    priority INTEGER,
    status TEXT CHECK(status IN ('PENDING', 'IN_TRANSIT', 'COMPLETED', 'FAILED', 'CANCELLED')),
    FOREIGN KEY (recommendation_id) REFERENCES COORDINATION_RECOMMENDATION(recommendation_id),
    FOREIGN KEY (vehicle_id) REFERENCES VEHICLE(vehicle_id),
    FOREIGN KEY (from_hub_id) REFERENCES MOBILITY_HUB(hub_id),
    FOREIGN KEY (to_hub_id) REFERENCES MOBILITY_HUB(hub_id),
    FOREIGN KEY (created_by_user_id) REFERENCES "USER"(user_id)
);

-- 7. SIMULATION MODULE
CREATE TABLE SIMULATION_RUN (
    run_id TEXT PRIMARY KEY, -- UUID
    started_by_user_id TEXT NOT NULL,
    scenario_name TEXT,
    scenario_config TEXT, -- JSON mapping
    baseline_unmet_demand INTEGER,
    projected_unmet_demand INTEGER,
    simulation_outcome TEXT,
    status TEXT,
    started_at TEXT, -- TIMESTAMP
    FOREIGN KEY (started_by_user_id) REFERENCES "USER"(user_id)
);

CREATE TABLE COORDINATION_RECOMMENDATION (
    recommendation_id TEXT PRIMARY KEY, -- UUID
    simulation_run_id TEXT NOT NULL,
    source_hub_id TEXT,
    destination_hub_id TEXT,
    recommendation_type TEXT,
    details TEXT, -- JSON mapping
    status TEXT,
    FOREIGN KEY (simulation_run_id) REFERENCES SIMULATION_RUN(run_id),
    FOREIGN KEY (source_hub_id) REFERENCES MOBILITY_HUB(hub_id),
    FOREIGN KEY (destination_hub_id) REFERENCES MOBILITY_HUB(hub_id)
);

-- 8. INDEXES & CONCURRENCY CONSTRAINTS (NFR-01 & NFR-05)

-- Partial Unique Indexes: Concurrency Double-Booking Protection (NFR-05 & Business Rule 1)
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_vehicle_res 
ON VEHICLE_RESERVATION (vehicle_id) 
WHERE status IN ('CONFIRMED', 'PENDING', 'RESERVED');

CREATE UNIQUE INDEX IF NOT EXISTS idx_active_student_res 
ON VEHICLE_RESERVATION (student_id) 
WHERE status IN ('CONFIRMED', 'PENDING', 'RESERVED');

CREATE UNIQUE INDEX IF NOT EXISTS idx_active_parking_space_res 
ON PARKING_RESERVATION (parking_space_id) 
WHERE status IN ('RESERVED', 'CHECKED_IN');

CREATE UNIQUE INDEX IF NOT EXISTS idx_active_charging_point_session 
ON CHARGING_SESSION (charging_point_id) 
WHERE status = 'CHARGING';

-- Foreign Key & Query Performance Indexes (NFR-01)
CREATE INDEX IF NOT EXISTS idx_vehicle_hub_status ON VEHICLE (current_hub_id, status, ownership_type);
CREATE INDEX IF NOT EXISTS idx_vehicle_battery ON VEHICLE (battery_pct);

CREATE INDEX IF NOT EXISTS idx_hub_resource_hub ON HUB_RESOURCE (hub_id, resource_type);

CREATE INDEX IF NOT EXISTS idx_trip_student_status ON TRIP (student_id, status);
CREATE INDEX IF NOT EXISTS idx_trip_origin_dest ON TRIP (origin_hub_id, destination_hub_id);

CREATE INDEX IF NOT EXISTS idx_reservation_vehicle_status ON VEHICLE_RESERVATION (vehicle_id, status);
CREATE INDEX IF NOT EXISTS idx_reservation_student_status ON VEHICLE_RESERVATION (student_id, status);
CREATE INDEX IF NOT EXISTS idx_reservation_trip ON VEHICLE_RESERVATION (trip_id);
CREATE INDEX IF NOT EXISTS idx_reservation_deadline ON VEHICLE_RESERVATION (pickup_deadline_at);

CREATE INDEX IF NOT EXISTS idx_rental_reservation ON RENTAL (reservation_id);
CREATE INDEX IF NOT EXISTS idx_rental_origin_hub ON RENTAL (origin_hub_id);
CREATE INDEX IF NOT EXISTS idx_rental_return_hub ON RENTAL (return_hub_id);
CREATE INDEX IF NOT EXISTS idx_rental_status ON RENTAL (status);

CREATE INDEX IF NOT EXISTS idx_parking_res_space ON PARKING_RESERVATION (parking_space_id, status);
CREATE INDEX IF NOT EXISTS idx_parking_res_user ON PARKING_RESERVATION (user_id, status);

CREATE INDEX IF NOT EXISTS idx_charging_session_point ON CHARGING_SESSION (charging_point_id, status);
CREATE INDEX IF NOT EXISTS idx_charging_session_user ON CHARGING_SESSION (user_id, status);

CREATE INDEX IF NOT EXISTS idx_telemetry_vehicle_ts ON SENSOR_TELEMETRY (vehicle_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_resource_ts ON SENSOR_TELEMETRY (resource_id, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_incident_hub_status ON OPERATIONAL_INCIDENT (hub_id, status);
CREATE INDEX IF NOT EXISTS idx_incident_vehicle ON OPERATIONAL_INCIDENT (vehicle_id);
CREATE INDEX IF NOT EXISTS idx_incident_resource ON OPERATIONAL_INCIDENT (resource_id);

CREATE INDEX IF NOT EXISTS idx_redist_task_status ON REDISTRIBUTION_TASK (status);
CREATE INDEX IF NOT EXISTS idx_redist_task_vehicle ON REDISTRIBUTION_TASK (vehicle_id);
