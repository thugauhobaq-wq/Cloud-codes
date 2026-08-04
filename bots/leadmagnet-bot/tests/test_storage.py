from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from conftest import add_magnet
from leadmagnet.models import KIND_FILE
from leadmagnet.storage import DuplicateCode, Storage

# ── магниты ───────────────────────────────────────────────────────────────


async def test_code_is_stored_normalized(storage: Storage):
    magnet = await add_magnet(storage, "Чек-Лист!")
    assert magnet.code == "чеклист"

    # И находится по любому написанию.
    assert (await storage.magnet_by_code("ЧЕКЛИСТ")).id == magnet.id
    assert (await storage.magnet_by_code("чёклист")).id == magnet.id


async def test_duplicate_code_is_rejected(storage: Storage):
    await add_magnet(storage, "чеклист")
    with pytest.raises(DuplicateCode):
        await add_magnet(storage, "Чек-лист")


async def test_empty_code_is_rejected(storage: Storage):
    with pytest.raises(ValueError):
        await add_magnet(storage, "!!!")


async def test_magnet_without_content_is_not_ready(storage: Storage):
    magnet = await storage.add_magnet("файл", "Файл", kind=KIND_FILE)
    assert not magnet.ready

    await storage.update_magnet(magnet.id, file_id="BQACAgIAAx0")
    assert (await storage.get_magnet(magnet.id)).ready


async def test_disabled_magnet_is_not_found_by_code(storage: Storage):
    magnet = await add_magnet(storage)
    await storage.update_magnet(magnet.id, active=0)

    assert await storage.magnet_by_code("чеклист") is None
    # Админу он по-прежнему доступен — иначе его не включить обратно.
    assert await storage.magnet_by_code("чеклист", only_active=False) is not None


# ── подписчики ────────────────────────────────────────────────────────────


async def test_lead_keeps_the_first_source(storage: Storage):
    """Важно, откуда человек пришёл впервые, а не по какой ссылке кликнул потом."""
    await storage.upsert_lead(1, name="Иван", source="instagram")
    await storage.upsert_lead(1, name="Иван Петров", source="vk")

    lead = await storage.get_lead(1)
    assert lead.source == "instagram"
    assert lead.name == "Иван Петров"


async def test_blocked_lead_returns_to_the_base_when_they_come_back(storage: Storage):
    await storage.upsert_lead(1, name="Иван")
    await storage.mark_blocked(1)
    assert (await storage.get_lead(1)).blocked
    assert await storage.count_leads() == 0

    await storage.upsert_lead(1, name="Иван")
    assert not (await storage.get_lead(1)).blocked
    assert await storage.count_leads() == 1


async def test_leads_can_be_segmented_by_magnet(storage: Storage):
    checklist = await add_magnet(storage, "чеклист")
    guide = await add_magnet(storage, "гайд", title="Гайд")

    await storage.upsert_lead(1, name="Первый")
    await storage.upsert_lead(2, name="Второй")
    await storage.record_delivery(1, checklist.id)
    await storage.record_delivery(2, guide.id)

    got_checklist = await storage.list_leads(magnet_code="чеклист")
    assert [item.tg_id for item in got_checklist] == [1]
    assert len(await storage.list_leads()) == 2


async def test_segment_does_not_duplicate_leads_with_repeat_deliveries(storage: Storage):
    magnet = await add_magnet(storage)
    await storage.upsert_lead(1, name="Иван")
    await storage.record_delivery(1, magnet.id)
    await storage.record_delivery(1, magnet.id)

    assert len(await storage.list_leads(magnet_code="чеклист")) == 1


async def test_blocked_leads_are_excluded_from_broadcasts(storage: Storage):
    await storage.upsert_lead(1, name="Активный")
    await storage.upsert_lead(2, name="Заблокировал")
    await storage.mark_blocked(2)

    assert [item.tg_id for item in await storage.list_leads()] == [1]
    assert len(await storage.list_leads(include_blocked=True)) == 2


# ── выдачи и догоняющие ───────────────────────────────────────────────────


async def test_repeat_delivery_is_recorded_too(storage: Storage):
    magnet = await add_magnet(storage)
    await storage.upsert_lead(1)
    await storage.record_delivery(1, magnet.id)

    assert await storage.already_delivered(1, magnet.id)
    await storage.record_delivery(1, magnet.id)
    assert len(await storage.lead_deliveries(1)) == 2


async def test_due_followups_waits_for_the_delay(storage: Storage):
    magnet = await add_magnet(storage, followup="Ну как чек-лист?")
    await storage.upsert_lead(1)
    await storage.record_delivery(1, magnet.id)

    now = datetime.now(UTC)
    assert await storage.due_followups(now, timedelta(hours=24)) == []
    assert len(await storage.due_followups(now + timedelta(hours=25), timedelta(hours=24))) == 1


async def test_followup_is_sent_once(storage: Storage):
    magnet = await add_magnet(storage, followup="Ну как чек-лист?")
    await storage.upsert_lead(1)
    delivery = await storage.record_delivery(1, magnet.id)

    later = datetime.now(UTC) + timedelta(hours=25)
    await storage.mark_followup_sent(delivery.id)
    assert await storage.due_followups(later, timedelta(hours=24)) == []


async def test_magnet_without_followup_never_shows_up(storage: Storage):
    """Пустой догон — осознанный отказ от второго касания, а не недоделка."""
    magnet = await add_magnet(storage, followup="")
    await storage.upsert_lead(1)
    await storage.record_delivery(1, magnet.id)

    later = datetime.now(UTC) + timedelta(days=3)
    assert await storage.due_followups(later, timedelta(hours=24)) == []


# ── статистика ────────────────────────────────────────────────────────────


async def test_stats_counts_leads_and_deliveries(storage: Storage):
    magnet = await add_magnet(storage)
    await storage.upsert_lead(1, name="Первый")
    await storage.upsert_lead(2, name="Второй")
    await storage.record_delivery(1, magnet.id, source="instagram")
    await storage.record_delivery(2, magnet.id, source="instagram")
    await storage.mark_blocked(2)

    stats = await storage.stats(30)
    assert stats["total_leads"] == 2
    assert stats["new_leads"] == 2
    assert stats["deliveries"] == 2
    assert stats["blocked"] == 1


async def test_by_source_groups_and_labels_empty_source(storage: Storage):
    magnet = await add_magnet(storage)
    await storage.upsert_lead(1)
    await storage.record_delivery(1, magnet.id, source="instagram")
    await storage.record_delivery(1, magnet.id, source="instagram")
    await storage.record_delivery(1, magnet.id)

    assert await storage.by_source(30) == [("instagram", 2), ("без метки", 1)]


async def test_by_magnet_includes_magnets_without_deliveries(storage: Storage):
    """Ноль выдач — тоже результат: видно, что материал не работает."""
    popular = await add_magnet(storage, "чеклист")
    await add_magnet(storage, "гайд", title="Гайд")
    await storage.upsert_lead(1)
    await storage.record_delivery(1, popular.id)

    assert await storage.by_magnet(30) == [("чеклист", "Чек-лист", 1), ("гайд", "Гайд", 0)]


async def test_export_rows_join_magnet_codes(storage: Storage):
    checklist = await add_magnet(storage, "чеклист")
    guide = await add_magnet(storage, "гайд", title="Гайд")
    await storage.upsert_lead(1, name="Иван", username="ivan", source="vk")
    await storage.record_delivery(1, checklist.id)
    await storage.record_delivery(1, guide.id)

    rows = await storage.export_rows()
    assert len(rows) == 1
    assert rows[0][1] == "Иван"
    assert "чеклист" in rows[0][6] and "гайд" in rows[0][6]
