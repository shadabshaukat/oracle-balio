-- BALIO schema version 1. Applied by `balio deploy`, not directly by SQL*Plus.
-- Ordinary heap tables, conventional logged inserts and primary-key lookups.
-- Every object is owned by the connected, dedicated benchmark schema.
-- Statements are separated by a slash on its own line for the Python loader.

-- BALIO_OBJECT: BALIO_META TABLE
CREATE TABLE BALIO_META (
    singleton NUMBER NOT NULL,
    magic VARCHAR2(32 BYTE) NOT NULL,
    schema_version NUMBER NOT NULL,
    status VARCHAR2(12 BYTE) NOT NULL,
    hot_rows NUMBER NOT NULL,
    cold_rows NUMBER NOT NULL,
    payload_bytes NUMBER NOT NULL,
    hot_seeded NUMBER NOT NULL,
    cold_seeded NUMBER NOT NULL,
    created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT BALIO_META_PK PRIMARY KEY (singleton),
    CONSTRAINT BALIO_META_ONE CHECK (singleton = 1),
    CONSTRAINT BALIO_META_MAGIC CHECK (magic = 'BALIO_DATASET_V1'),
    CONSTRAINT BALIO_META_STATE CHECK (status IN ('EMPTY', 'SEEDING', 'READY'))
) LOGGING NOCOMPRESS;
/
-- BALIO_OBJECT: BALIO_HOT TABLE
CREATE TABLE BALIO_HOT (
    id NUMBER NOT NULL,
    payload VARCHAR2(2000 BYTE) NOT NULL,
    touch NUMBER DEFAULT 0 NOT NULL,
    CONSTRAINT BALIO_HOT_PK PRIMARY KEY (id)
) LOGGING NOCOMPRESS;
/
-- BALIO_OBJECT: BALIO_COLD TABLE
CREATE TABLE BALIO_COLD (
    id NUMBER NOT NULL,
    payload VARCHAR2(2000 BYTE) NOT NULL,
    touch NUMBER DEFAULT 0 NOT NULL,
    CONSTRAINT BALIO_COLD_PK PRIMARY KEY (id)
) LOGGING NOCOMPRESS;
/
-- BALIO_OBJECT: BALIO_RUNNER PACKAGE
CREATE OR REPLACE PACKAGE BALIO_RUNNER AUTHID DEFINER AS
    -- BALIO_MANAGED_SCHEMA_1
    PROCEDURE RUN_BATCH (
        p_operations IN PLS_INTEGER,
        p_cold_pct IN NUMBER,
        p_update_pct IN NUMBER,
        p_seed IN OUT NUMBER,
        p_worker_id IN PLS_INTEGER,
        p_workers IN PLS_INTEGER,
        p_hot_rows IN NUMBER,
        p_cold_rows IN NUMBER,
        p_commit_every IN PLS_INTEGER,
        p_hot_ops OUT NUMBER,
        p_cold_ops OUT NUMBER,
        p_read_ops OUT NUMBER,
        p_update_ops OUT NUMBER,
        p_commits OUT NUMBER,
        p_checksum OUT NUMBER
    );
END BALIO_RUNNER;
/
-- BALIO_OBJECT: BALIO_RUNNER PACKAGE BODY
CREATE OR REPLACE PACKAGE BODY BALIO_RUNNER AS
    -- BALIO_MANAGED_SCHEMA_1
    PROCEDURE RUN_BATCH (
        p_operations IN PLS_INTEGER,
        p_cold_pct IN NUMBER,
        p_update_pct IN NUMBER,
        p_seed IN OUT NUMBER,
        p_worker_id IN PLS_INTEGER,
        p_workers IN PLS_INTEGER,
        p_hot_rows IN NUMBER,
        p_cold_rows IN NUMBER,
        p_commit_every IN PLS_INTEGER,
        p_hot_ops OUT NUMBER,
        p_cold_ops OUT NUMBER,
        p_read_ops OUT NUMBER,
        p_update_ops OUT NUMBER,
        p_commits OUT NUMBER,
        p_checksum OUT NUMBER
    ) IS
        -- NUMBER deliberately avoids PLS_INTEGER overflow during multiplication.
        v_state NUMBER := p_seed;
        v_rows NUMBER;
        v_first NUMBER;
        v_span NUMBER;
        v_id NUMBER;
        v_payload VARCHAR2(2000 BYTE);
        v_touch NUMBER;
        v_cold BOOLEAN;
        v_update BOOLEAN;
        v_uncommitted PLS_INTEGER := 0;

        FUNCTION next_random RETURN NUMBER IS
        BEGIN
            v_state := MOD(v_state * 16807, 2147483647);
            RETURN v_state / 2147483647;
        END;
    BEGIN
        IF p_operations IS NULL OR p_operations < 1 OR p_operations > 1000000
           OR p_cold_pct IS NULL OR p_cold_pct < 0 OR p_cold_pct > 100
           OR p_update_pct IS NULL OR p_update_pct < 0 OR p_update_pct > 100
           OR p_seed IS NULL OR p_seed < 1 OR p_seed > 2147483646
           OR p_seed <> TRUNC(p_seed)
           OR p_workers IS NULL OR p_workers < 1
           OR p_worker_id IS NULL OR p_worker_id < 0 OR p_worker_id >= p_workers
           OR p_hot_rows IS NULL OR p_hot_rows < 0 OR p_hot_rows > 2147483646
           OR p_hot_rows <> TRUNC(p_hot_rows)
           OR p_cold_rows IS NULL OR p_cold_rows < 0 OR p_cold_rows > 2147483646
           OR p_cold_rows <> TRUNC(p_cold_rows)
           OR p_commit_every IS NULL OR p_commit_every < 1 THEN
            RAISE_APPLICATION_ERROR(-20001, 'Invalid BALIO batch parameters');
        END IF;
        IF (p_cold_pct < 100 AND p_hot_rows = 0)
           OR (p_cold_pct > 0 AND p_cold_rows = 0) THEN
            RAISE_APPLICATION_ERROR(-20002, 'Active BALIO pool has no rows');
        END IF;
        IF p_update_pct > 0 AND
           ((p_cold_pct < 100 AND p_hot_rows < p_workers)
            OR (p_cold_pct > 0 AND p_cold_rows < p_workers)) THEN
            RAISE_APPLICATION_ERROR(-20003, 'Update pools need at least one row per worker');
        END IF;

        p_hot_ops := 0;
        p_cold_ops := 0;
        p_read_ops := 0;
        p_update_ops := 0;
        p_commits := 0;
        p_checksum := 0;
        FOR i IN 1 .. p_operations LOOP
            v_cold := next_random * 100 < p_cold_pct;
            v_update := next_random * 100 < p_update_pct;
            IF v_cold THEN
                v_rows := p_cold_rows;
                p_cold_ops := p_cold_ops + 1;
            ELSE
                v_rows := p_hot_rows;
                p_hot_ops := p_hot_ops + 1;
            END IF;
            IF v_update THEN
                -- Contiguous worker slices prevent same-row update contention.
                -- Boundary rows can still share a block; RAC block traffic is real.
                v_first := FLOOR(v_rows * p_worker_id / p_workers) + 1;
                v_span := FLOOR(v_rows * (p_worker_id + 1) / p_workers) - v_first + 1;
            ELSE
                v_first := 1;
                v_span := v_rows;
            END IF;
            v_id := v_first + FLOOR(next_random * v_span);

            IF v_update THEN
                IF v_cold THEN
                    UPDATE /*+ INDEX(t BALIO_COLD_PK) NO_PARALLEL(t) */ BALIO_COLD t
                       SET touch = MOD(touch + 1, 1000000000)
                     WHERE id = v_id
                     RETURNING payload, touch INTO v_payload, v_touch;
                ELSE
                    UPDATE /*+ INDEX(t BALIO_HOT_PK) NO_PARALLEL(t) */ BALIO_HOT t
                       SET touch = MOD(touch + 1, 1000000000)
                     WHERE id = v_id
                     RETURNING payload, touch INTO v_payload, v_touch;
                END IF;
                IF SQL%ROWCOUNT <> 1 THEN
                    RAISE_APPLICATION_ERROR(-20004, 'BALIO row missing; reseed a clean schema');
                END IF;
                p_update_ops := p_update_ops + 1;
                v_uncommitted := v_uncommitted + 1;
            ELSE
                IF v_cold THEN
                    SELECT /*+ INDEX(t BALIO_COLD_PK) NO_RESULT_CACHE NO_PARALLEL(t) */ payload, touch
                      INTO v_payload, v_touch FROM BALIO_COLD t WHERE id = v_id;
                ELSE
                    SELECT /*+ INDEX(t BALIO_HOT_PK) NO_RESULT_CACHE NO_PARALLEL(t) */ payload, touch
                      INTO v_payload, v_touch FROM BALIO_HOT t WHERE id = v_id;
                END IF;
                p_read_ops := p_read_ops + 1;
            END IF;
            -- The payload is consumed and absent from the index. A read cannot
            -- accidentally become an index-only COUNT or an unconsumed SELECT.
            p_checksum := MOD(p_checksum + LENGTHB(v_payload) + v_touch
                              + ASCII(SUBSTR(v_payload, 1, 1))
                              + ASCII(SUBSTR(v_payload, -1, 1)), 2147483647);
            IF v_uncommitted >= p_commit_every THEN
                COMMIT;
                p_commits := p_commits + 1;
                v_uncommitted := 0;
            END IF;
        END LOOP;
        IF v_uncommitted > 0 THEN
            COMMIT;
            p_commits := p_commits + 1;
        END IF;
        p_seed := v_state;
    END RUN_BATCH;
END BALIO_RUNNER;
/
