import tempfile
from pathlib import Path
from pkm import store
from pkm.models import Section, Disposition, Item

def test_add_and_read_items():
    with tempfile.TemporaryDirectory() as tmpdir:
        active_path = Path(tmpdir) / "active.md"
        
        # Test adding items into flight, waiting, scratch
        store.add_item(active_path, "Task 1", section=Section.FLIGHT, as_checkbox=True)
        store.add_item(active_path, "Task 2", section=Section.FLIGHT, as_checkbox=True)
        store.add_item(active_path, "Blocked on Ali", section=Section.WAITING, as_checkbox=False)
        store.add_item(active_path, "A command to run", section=Section.SCRATCH, as_checkbox=False)
        
        flight_items = store.read_items(active_path, Section.FLIGHT)
        assert len(flight_items) == 2
        assert flight_items[0].clean_text == "Task 1"
        assert flight_items[1].clean_text == "Task 2"
        
        waiting_items = store.read_items(active_path, Section.WAITING)
        assert len(waiting_items) == 1
        assert waiting_items[0].clean_text == "Blocked on Ali"
        
        scratch_items = store.read_items(active_path, Section.SCRATCH)
        assert len(scratch_items) == 1
        assert scratch_items[0].clean_text == "A command to run"


def test_remove_item():
    with tempfile.TemporaryDirectory() as tmpdir:
        active_path = Path(tmpdir) / "active.md"
        store.add_item(active_path, "Task 1", section=Section.FLIGHT)
        store.add_item(active_path, "Task 2", section=Section.FLIGHT)
        
        items = store.read_items(active_path, Section.FLIGHT)
        assert len(items) == 2
        
        removed = store.remove_item(active_path, items[0])
        assert removed is True
        
        remaining = store.read_items(active_path, Section.FLIGHT)
        assert len(remaining) == 1
        assert remaining[0].clean_text == "Task 2"


def test_move_item_within_active():
    with tempfile.TemporaryDirectory() as tmpdir:
        active_path = Path(tmpdir) / "active.md"
        store.add_item(active_path, "Task to block", section=Section.FLIGHT)
        
        items = store.read_items(active_path, Section.FLIGHT)
        assert len(items) == 1
        
        store.move_item(items[0], from_file=active_path, to_file=active_path, to_section=Section.WAITING)
        
        assert len(store.read_items(active_path, Section.FLIGHT)) == 0
        waiting = store.read_items(active_path, Section.WAITING)
        assert len(waiting) == 1
        assert waiting[0].clean_text == "Task to block"


def test_move_item_to_log_with_disposition():
    with tempfile.TemporaryDirectory() as tmpdir:
        active_path = Path(tmpdir) / "active.md"
        log_path = Path(tmpdir) / "log.md"
        
        store.add_item(active_path, "Ship v2", section=Section.FLIGHT)
        items = store.read_items(active_path, Section.FLIGHT)
        
        store.move_item(items[0], from_file=active_path, to_file=log_path, disposition=Disposition.COMPLETED)
        
        assert len(store.read_items(active_path, Section.FLIGHT)) == 0
        log_items = store.read_items(log_path)
        assert len(log_items) == 1
        assert "completed [" in log_items[0].clean_text
        assert "Ship v2" in log_items[0].clean_text


def test_bankrupt_inbox():
    with tempfile.TemporaryDirectory() as tmpdir:
        inbox_path = Path(tmpdir) / "inbox.md"
        someday_path = Path(tmpdir) / "someday.md"
        
        store.append_to_inbox(inbox_path, "Idea 1")
        store.append_to_inbox(inbox_path, "Idea 2")
        
        count = store.bankrupt_inbox(inbox_path, someday_path)
        assert count == 2
        
        # Inbox should now be empty of bullet items
        assert len(store.read_items(inbox_path)) == 0
        assert len(store.read_items(someday_path)) == 2


def test_tags_and_provenance():
    with tempfile.TemporaryDirectory() as tmpdir:
        vault = Path(tmpdir)
        inbox_path = vault / "inbox.md"
        
        store.append_to_inbox_with_tag(inbox_path, "Review auth PR", "quotient")
        store.append_to_inbox_with_url(inbox_path, "Check deepmind paper", url="https://arxiv.org/abs/1234", tag="research")
        
        items = store.read_items(inbox_path)
        assert len(items) == 2
        assert items[0].tag == "quotient"
        assert items[1].tag == "research"
        assert "https://arxiv.org/abs/1234" in items[1].clean_text
        
        tags = store.get_all_tags(vault)
        assert tags["quotient"] == 1
        assert tags["research"] == 1
