import unittest

from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine, select

from models import Project, ProjectCategoryOption
from routers.project_categories import (
    ProjectCategoryCreate,
    ProjectCategoryUpdate,
    create_project_category,
    delete_project_category,
    list_project_categories,
    project_category_names,
    update_project_category,
)


class ProjectCategoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        for order, name in enumerate(["series", "concert"]):
            self.session.add(ProjectCategoryOption(name=name, label=name.title(), sort_order=order))
        self.session.commit()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def ids(self):
        return {row["value"]: row["id"] for row in list_project_categories(self.session)}

    def test_falls_back_to_built_in_list_when_none_saved(self):
        for row in self.session.exec(select(ProjectCategoryOption)).all():
            self.session.delete(row)
        self.session.commit()
        self.assertIn("series", project_category_names(self.session))

    def test_add_and_reorder(self):
        added = create_project_category(ProjectCategoryCreate(name="  Fan  Event "), self.session)
        self.assertEqual((added["value"], added["label"]), ("fan event", "Fan Event"))
        ids = self.ids()
        update_project_category(ids["fan event"], ProjectCategoryUpdate(sort_order=-1), self.session)
        self.assertEqual(project_category_names(self.session), ["fan event", "series", "concert"])

    def test_duplicates_are_rejected(self):
        with self.assertRaises(HTTPException):
            create_project_category(ProjectCategoryCreate(name="Concert"), self.session)

    def test_rename_moves_projects_and_series_is_locked(self):
        self.session.add(Project(title="Show", category="concert"))
        self.session.commit()
        ids = self.ids()
        update_project_category(ids["concert"], ProjectCategoryUpdate(name="live show", label="Live Show"), self.session)
        self.assertEqual(self.session.exec(select(Project.category)).first(), "live show")
        with self.assertRaises(HTTPException):
            update_project_category(ids["series"], ProjectCategoryUpdate(name="shows"), self.session)
        # the label of series can still change
        self.assertEqual(update_project_category(ids["series"], ProjectCategoryUpdate(label="TV Series"), self.session)["label"], "TV Series")

    def test_categories_can_be_hidden_and_shown(self):
        ids = self.ids()
        hidden = update_project_category(ids["concert"], ProjectCategoryUpdate(is_visible=False), self.session)
        self.assertFalse(hidden["is_visible"])
        self.assertIn("concert", project_category_names(self.session))  # still a valid category for projects
        self.assertTrue(update_project_category(ids["concert"], ProjectCategoryUpdate(is_visible=True), self.session)["is_visible"])

    def test_delete_rules(self):
        self.session.add(Project(title="Show", category="concert"))
        self.session.commit()
        ids = self.ids()
        with self.assertRaises(HTTPException):
            delete_project_category(ids["series"], self.session)
        with self.assertRaises(HTTPException):
            delete_project_category(ids["concert"], self.session)
        extra = create_project_category(ProjectCategoryCreate(name="spare"), self.session)
        delete_project_category(extra["id"], self.session)
        self.assertNotIn("spare", project_category_names(self.session))


if __name__ == "__main__":
    unittest.main()
