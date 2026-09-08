import unittest
from pydantic import ValidationError
from sqlmodel import Session, SQLModel, create_engine

from character_map import CharacterMapData
from models import Project, Author
from routers.projects import ProjectUpdate, update_project, _serialize_project


def chart():
    return {
        "characters": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}],
        "relationships": [{"id": "ab", "source": "a", "target": "b", "changes": [
            {"episode": 1, "label": "Strangers"},
            {"episode": 3, "label": "Friends"},
        ]}],
    }


class CharacterMapTests(unittest.TestCase):
    def test_chart_text_and_groups_round_trip_and_validate_members(self):
        data = chart()
        data["texts"] = {"heading": "คนในร้าน", "decoration": ""}
        data["groups"] = [{"id": "bakery", "label": "Bakery team", "shape": "circle", "character_ids": ["a", "b"]}]
        restored = CharacterMapData.model_validate_json(CharacterMapData.model_validate(data).model_dump_json())
        self.assertEqual(restored.texts["heading"], "คนในร้าน")
        self.assertEqual(restored.texts["decoration"], "")
        self.assertEqual(restored.groups[0].character_ids, ["a", "b"])
        data["groups"][0]["character_ids"].append("missing")
        with self.assertRaises(ValidationError):
            CharacterMapData.model_validate(data)

    def test_thai_name_and_database_or_manual_actor(self):
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            author = Author(name="Actor from database")
            project = Project(title="Test series")
            session.add_all([author, project])
            session.commit()
            data = chart()
            data["characters"][0].update(thai_name="ขนมปัง", author_id=author.id, actor="Old name")
            saved = update_project(project.id, ProjectUpdate(character_map=data), session)
            self.assertEqual(saved["character_map"]["characters"][0]["thai_name"], "ขนมปัง")
            self.assertEqual(saved["character_map"]["characters"][0]["actor"], author.name)
            author.name = "Updated actor name"
            session.add(author)
            session.commit()
            reloaded = _serialize_project(session, project)
            self.assertEqual(reloaded["character_map"]["characters"][0]["actor"], author.name)
            data["characters"][0].update(author_id=None, actor="Manual actor")
            saved = update_project(project.id, ProjectUpdate(character_map=data), session)
            self.assertIsNone(saved["character_map"]["characters"][0]["author_id"])
            self.assertEqual(saved["character_map"]["characters"][0]["actor"], "Manual actor")
        engine.dispose()

    def test_hide_reload_and_restore_preserves_chart(self):
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            project = Project(title="Test series", category="series")
            session.add(project)
            session.commit()
            project_id = project.id
            saved = update_project(project_id, ProjectUpdate(character_map=chart()), session)
            update_project(project_id, ProjectUpdate(show_character_map=False), session)
        with Session(engine) as session:
            reloaded = _serialize_project(session, session.get(Project, project_id))
            self.assertFalse(reloaded["show_character_map"])
            self.assertEqual(reloaded["character_map"], saved["character_map"])
            restored = update_project(project_id, ProjectUpdate(show_character_map=True), session)
            self.assertTrue(restored["show_character_map"])
            self.assertEqual(restored["character_map"], saved["character_map"])
            emptied = update_project(project_id, ProjectUpdate(character_map={"characters": [], "relationships": []}), session)
            self.assertEqual(emptied["character_map"]["characters"], [])
        engine.dispose()

    def test_reject_dangling_relationship(self):
        data = chart()
        data["relationships"][0]["target"] = "missing"
        with self.assertRaises(ValidationError):
            CharacterMapData.model_validate(data)

    def test_reject_duplicate_episode_changes(self):
        data = chart()
        data["relationships"][0]["changes"][1]["episode"] = 1
        with self.assertRaises(ValidationError):
            CharacterMapData.model_validate(data)

    def test_reject_unsafe_portrait(self):
        data = chart()
        data["characters"][0]["photo"] = "javascript:alert(1)"
        with self.assertRaises(ValidationError):
            CharacterMapData.model_validate(data)


if __name__ == "__main__":
    unittest.main()
