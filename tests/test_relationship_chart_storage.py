import unittest
from sqlalchemy import text
from sqlmodel import SQLModel, Session, create_engine
from database import migrate_relationship_charts
from models import Project, ProjectRelationshipChart
from routers.projects import ProjectUpdate, update_project, delete_project
from tests.test_relationship_chart import chart


class RelationshipChartStorageTests(unittest.TestCase):
    def test_migration_save_and_delete(self):
        engine = create_engine('sqlite://')
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            project = Project(title='Test', relationship_chart_json='{"characters": [], "relationships": []}')
            session.add(project)
            session.commit()
            project_id = project.id
        with engine.begin() as conn:
            migrate_relationship_charts(conn)
            conn.execute(text("UPDATE project_relationship_chart SET data_json = '{\"characters\": [], \"relationships\": [], \"texts\": {}}'"))
            migrate_relationship_charts(conn)
        with Session(engine) as session:
            self.assertIn('texts', session.get(ProjectRelationshipChart, project_id).data_json)
            update_project(project_id, ProjectUpdate(relationship_chart=chart()), session)
            self.assertIsNone(session.get(Project, project_id).relationship_chart_json)
            self.assertIn('Strangers', session.get(ProjectRelationshipChart, project_id).data_json)
            delete_project(project_id, session)
            self.assertIsNone(session.get(ProjectRelationshipChart, project_id))

    def test_migration_copies_the_legacy_table(self):
        engine = create_engine('sqlite://')
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            project = Project(title='Legacy chart')
            session.add(project)
            session.commit()
            project_id = project.id
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE project_character_map (project_id INTEGER PRIMARY KEY, data_json TEXT NOT NULL)"))
            conn.execute(
                text("INSERT INTO project_character_map (project_id, data_json) VALUES (:project_id, :data_json)"),
                {"project_id": project_id, "data_json": '{"characters": [], "relationships": []}'},
            )
            migrate_relationship_charts(conn)
        with Session(engine) as session:
            migrated = session.get(ProjectRelationshipChart, project_id)
            self.assertIsNotNone(migrated)
            self.assertIn('relationships', migrated.data_json)
        engine.dispose()
