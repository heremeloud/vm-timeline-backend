from typing import List, Literal, Optional, Annotated, Dict
from pydantic import BaseModel, Field, field_validator, model_validator


class Character(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=120)
    thai_name: str = Field(default="", max_length=120)
    author_id: Optional[int] = Field(default=None, gt=0)
    role: str = Field(default="", max_length=200)
    actor: str = Field(default="", max_length=120)
    photo: str = Field(default="", max_length=2000)
    description: str = Field(default="", max_length=5000)
    tone: str = Field(default="#b76d79", max_length=20)
    size: Literal["large", "medium", "small"] = "medium"
    x: float = Field(default=50, ge=12, le=88)
    y: float = Field(default=30, ge=18, le=82)

    @field_validator("photo")
    @classmethod
    def safe_photo(cls, value):
        if value and not value.startswith(("https://", "http://", "/")):
            raise ValueError("Portrait must be an HTTP(S) URL or local image path")
        if value.startswith("//"):
            raise ValueError("Use a full HTTP(S) URL")
        return value


class RelationshipChange(BaseModel):
    episode: int = Field(ge=1, le=1000)
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=5000)
    color: str = Field(default="#a67c52", pattern=r"^#[0-9a-fA-F]{6}$")
    line_style: Literal["solid", "dashed", "dotted"] = "solid"
    curved: bool = False
    arrow_start: bool = False
    arrow_end: bool = False
    hidden: bool = False


class CharacterRelationship(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    source: str
    target: str
    changes: List[RelationshipChange] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_episodes(self):
        episodes = [change.episode for change in self.changes]
        if len(set(episodes)) != len(episodes):
            raise ValueError("Use only one change per episode for each relationship")
        return self


class CharacterGroup(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    label: str = Field(default="", max_length=120)
    thai_name: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=5000)
    shape: Literal["circle", "rectangle"] = "rectangle"
    color: str = Field(default="#a67c52", pattern=r"^#[0-9a-fA-F]{6}$")
    character_ids: List[str] = Field(default_factory=list, max_length=40)
    padding_x: float = Field(default=15, ge=4, le=40)
    padding_y: float = Field(default=22, ge=4, le=40)
    label_position: Literal["top", "bottom"] = "top"


class CharacterMapData(BaseModel):
    texts: Dict[str, Annotated[str, Field(max_length=1000)]] = Field(default_factory=dict, max_length=40)
    groups: List[CharacterGroup] = Field(default_factory=list, max_length=20)
    canvas_height: Optional[float] = Field(default=None, ge=360, le=3000)
    is_sample: bool = False
    episode_labels: Dict[str, Annotated[str, Field(max_length=120)]] = Field(default_factory=dict, max_length=1000)
    episodes: Optional[List[Annotated[int, Field(ge=1, le=1000)]]] = Field(default=None, max_length=1000)
    hidden_episodes: List[Annotated[int, Field(ge=1, le=1000)]] = Field(default_factory=list, max_length=1000)
    characters: List[Character] = Field(default_factory=list, max_length=40)
    relationships: List[CharacterRelationship] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def valid_connections(self):
        if self.episodes is not None and len(set(self.episodes)) != len(self.episodes):
            raise ValueError("Episode numbers must be unique")
        ids = [character.id for character in self.characters]
        if len(set(ids)) != len(ids):
            raise ValueError("Character IDs must be unique")
        if len({group.id for group in self.groups}) != len(self.groups):
            raise ValueError("Group IDs must be unique")
        for group in self.groups:
            if any(member not in ids for member in group.character_ids):
                raise ValueError("Groups must contain existing characters")
        relationship_ids = [relationship.id for relationship in self.relationships]
        if len(set(relationship_ids)) != len(relationship_ids):
            raise ValueError("Relationship IDs must be unique")
        for relationship in self.relationships:
            if self.episodes is not None and any(change.episode not in self.episodes for change in relationship.changes):
                raise ValueError("Relationship changes must use an episode in the character map")
            if relationship.source not in ids or relationship.target not in ids:
                raise ValueError("Relationships must connect existing characters")
            if relationship.source == relationship.target:
                raise ValueError("Select two different characters")
        return self
