from django.db import migrations


TABLES = ("school_slot", "school_request", "school_attendance", "school_lesson")
CHECKS = """
CREATE FUNCTION school_check_tenant_refs() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    tenant bigint;
    category bigint;
BEGIN
    IF TG_TABLE_NAME = 'school_lesson' THEN
        SELECT school_id INTO tenant FROM school_timetableweek WHERE id = NEW.week_id;
        IF NOT EXISTS (SELECT 1 FROM school_employee WHERE id = NEW.teacher_id AND school_id = tenant) THEN
            RAISE EXCEPTION 'lesson teacher outside school' USING ERRCODE = '23514';
        END IF;
    ELSIF TG_TABLE_NAME = 'school_slot' THEN
        SELECT t.school_id INTO tenant FROM school_dutyday d JOIN school_roster r ON r.id = d.roster_id
            JOIN school_dutytype t ON t.id = r.duty_type_id WHERE d.id = NEW.day_id;
        IF NOT EXISTS (SELECT 1 FROM school_employee WHERE id = NEW.original_id AND school_id = tenant)
            OR NOT EXISTS (SELECT 1 FROM school_employee WHERE id = NEW.assigned_id AND school_id = tenant) THEN
            RAISE EXCEPTION 'duty assignee outside school' USING ERRCODE = '23514';
        END IF;
    ELSIF TG_TABLE_NAME = 'school_request' THEN
        SELECT school_id INTO tenant FROM school_employee WHERE id = NEW.employee_id;
        IF (NEW.replacement_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM school_employee WHERE id = NEW.replacement_id AND school_id = tenant))
            OR (NEW.slot_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM school_slot s JOIN school_employee e ON e.id = s.assigned_id
                WHERE s.id = NEW.slot_id AND e.school_id = tenant)) THEN
            RAISE EXCEPTION 'request references another school' USING ERRCODE = '23514';
        END IF;
    ELSIF TG_TABLE_NAME = 'school_attendance' THEN
        SELECT t.school_id, t.id INTO tenant, category FROM school_slot s
            JOIN school_dutyday d ON d.id = s.day_id JOIN school_roster r ON r.id = d.roster_id
            JOIN school_dutytype t ON t.id = r.duty_type_id WHERE s.id = NEW.slot_id;
        IF NOT EXISTS (SELECT 1 FROM school_employee WHERE id = NEW.actual_id AND school_id = tenant)
            OR NOT EXISTS (SELECT 1 FROM school_membership m JOIN school_employee e ON e.id = m.employee_id
                WHERE m.id = NEW.confirmed_by_id AND e.school_id = tenant)
            OR (NEW.rate_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM school_rate WHERE id = NEW.rate_id AND duty_type_id = category)) THEN
            RAISE EXCEPTION 'attendance references another school or duty type' USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
"""


def install(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(CHECKS)
    for table in TABLES:
        schema_editor.execute(
            f"CREATE TRIGGER tenant_refs BEFORE INSERT OR UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION school_check_tenant_refs()"
        )


def remove(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in TABLES:
        schema_editor.execute(f"DROP TRIGGER tenant_refs ON {table}")
    schema_editor.execute("DROP FUNCTION school_check_tenant_refs()")


class Migration(migrations.Migration):
    dependencies = [("school", "0001_initial")]
    operations = [migrations.RunPython(install, remove)]
