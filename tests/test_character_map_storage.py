import unittest
from sqlalchemy import text
from sqlmodel import SQLModel, Session, create_engine
from database import migrate_character_maps
from models import Project, ProjectCharacterMap
from routers.projects import ProjectUpdate, update_project, delete_project
from tests.test_character_map import chart


class MapStorageTests(unittest.TestCase):
    def test_migration_save_and_delete(self):
        engine = create_engine('sqlite://')
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            project = Project(title='Test', character_map_json='{"characters": [], "relationships": []}')
            session.add(project)
            session.commit()
            project_id = project.id
        with engine.begin() as conn:
            migrate_character_maps(conn)
            conn.execute(text("UPDATE project_character_map SET data_json = '{\"characters\": [], \"relationships\": [], \"texts\": {}}'"))
            migrate_character_maps(conn)
        with Session(engine) as session:
            self.assertIn('texts', session.get(ProjectCharacterMap, project_id).data_json)
            update_project(project_id, ProjectUpdate(character_map=chart()), session)
            self.assertIsNone(session.get(Project, project_id).character_map_json)
            self.assertIn('Strangers', session.get(ProjectCharacterMap, project_id).data_json)
            delete_project(project_id, session)
            self.assertIsNone(session.get(ProjectCharacterMap, project_id))
