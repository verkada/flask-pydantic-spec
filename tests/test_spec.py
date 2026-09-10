from enum import Enum
from typing import Optional

import pytest
from flask import Flask
from flask.views import MethodView
from typing import List
from openapi_spec_validator import validate as validate_v3_spec
from pydantic.v1 import BaseModel, Field, StrictFloat

from flask_pydantic_spec import Response
from flask_pydantic_spec.flask_backend import FlaskBackend
from flask_pydantic_spec.types import FileResponse, Request, MultipartFormRequest
from flask_pydantic_spec import FlaskPydanticSpec
from flask_pydantic_spec.config import Config

from .common import get_paths


class ExampleModel(BaseModel):
    name: str = Field(strip_whitespace=True)
    age: int
    height: StrictFloat


class TypeEnum(str, Enum):
    foo = "foo"
    bar = "bar"


class ExampleQuery(BaseModel):
    query: str
    type: Optional[TypeEnum]


class ExampleNestedList(BaseModel):
    __root__: List[ExampleModel]


class ExampleNestedModel(BaseModel):
    example: ExampleModel


class ExampleDeepNestedModel(BaseModel):
    data: List["ExampleModel"]


def backend_app():
    return [
        ("flask", Flask(__name__)),
    ]


def test_spectree_init():
    spec = FlaskPydanticSpec(path="docs")
    conf = Config()

    assert spec.config.TITLE == conf.TITLE
    assert spec.config.PATH == "docs"


@pytest.mark.parametrize("name, app", backend_app())
def test_register(name, app):
    api = FlaskPydanticSpec(name)
    api.register(app)


@pytest.mark.parametrize("name, app", backend_app())
def test_spec_generate(name, app):
    api = FlaskPydanticSpec(
        name,
        app=app,
        title=f"{name}",
        info={"title": "override", "description": "api level description"},
        tags=[{"name": "lone", "description": "a lone api"}],
    )
    spec = api.spec

    assert spec["info"]["title"] == name
    assert spec["info"]["description"] == "api level description"
    assert spec["paths"] == {}
    assert spec["tags"] == []


api = FlaskPydanticSpec(
    "flask",
    tags=[{"name": "lone", "description": "a lone api"}],
    validation_error_code=400,
)
api_strict = FlaskPydanticSpec("flask", mode="strict")
api_greedy = FlaskPydanticSpec("flask", mode="greedy")
api_customize_backend = FlaskPydanticSpec(backend=FlaskBackend)


def create_app():
    app = Flask(__name__)

    @app.route("/foo")
    @api.validate()
    def foo():
        pass

    @app.route("/bar")
    @api_strict.validate()
    def bar():
        pass

    @app.route("/lone", methods=["GET"])
    def lone_get():
        pass

    @app.route("/lone", methods=["POST"])
    @api.validate(
        body=Request(ExampleModel),
        resp=Response(HTTP_200=ExampleNestedList, HTTP_400=ExampleNestedModel),
        tags=["lone"],
        deprecated=True,
    )
    def lone_post():
        pass

    @app.route("/query", methods=["GET"])
    @api.validate(query=ExampleQuery)
    def get_query():
        pass

    @app.route("/file")
    @api.validate(resp=FileResponse())
    def get_file():
        pass

    @app.route("/file", methods=["POST"])
    @api.validate(
        body=Request(content_type="application/octet-stream"),
        resp=Response(HTTP_200=None),
    )
    def post_file():
        pass

    @app.route("/multipart-file", methods=["POST"])
    @api.validate(
        body=MultipartFormRequest(ExampleModel), resp=Response(HTTP_200=ExampleModel)
    )
    def post_multipart_form():
        pass

    return app


def test_spec_bypass_mode():
    app = create_app()
    api.register(app)
    assert get_paths(api.spec) == [
        "/file",
        "/foo",
        "/lone",
        "/multipart-file",
        "/query",
    ]

    app = create_app()
    api_customize_backend.register(app)
    assert get_paths(api.spec) == [
        "/file",
        "/foo",
        "/lone",
        "/multipart-file",
        "/query",
    ]

    app = create_app()
    api_greedy.register(app)
    assert get_paths(api_greedy.spec) == [
        "/bar",
        "/file",
        "/foo",
        "/lone",
        "/multipart-file",
        "/query",
    ]

    app = create_app()
    api_strict.register(app)
    assert get_paths(api_strict.spec) == ["/bar"]


def test_two_endpoints_with_the_same_path():
    app = create_app()
    api.register(app)
    spec = api.spec

    http_methods = list(spec["paths"]["/lone"].keys())
    http_methods.sort()
    assert http_methods == ["get", "post"]


def test_valid_openapi_spec():
    # A dedicated FlaskPydanticSpec instance and fully-decorated app, rather
    # than the shared `api` singleton / create_app() fixture: `spec` is
    # cached forever after its first access (see spec_by_category()'s
    # `if not hasattr(self, "_spec")`), so reusing `api` here would just
    # return whatever an earlier test already cached. Separately,
    # create_app() deliberately includes undecorated/bare-decorator routes
    # (to exercise bypass-mode and route-merging behavior elsewhere in this
    # file), which produce empty `responses` objects that a strict OpenAPI
    # v3 validator correctly rejects (every operation must document at least
    # one response).
    app = Flask(__name__)
    local_api = FlaskPydanticSpec("flask")

    @app.route("/valid")
    @local_api.validate(resp=Response(HTTP_200=ExampleModel))
    def valid():
        pass

    local_api.register(app)
    validate_v3_spec(local_api.spec)


def test_openapi_tags():
    app = create_app()
    api.register(app)
    spec = api.spec

    assert spec["tags"][0]["name"] == "lone"
    assert spec["tags"][0]["description"] == "a lone api"


def test_openapi_deprecated():
    app = create_app()
    api.register(app)
    spec = api.spec

    assert spec["paths"]["/lone"]["post"]["deprecated"] == True
    assert "deprecated" not in spec["paths"]["/lone"]["get"]


api_publish_only = FlaskPydanticSpec("flask", mode="publish_only")


class WidgetView(MethodView):
    @api_publish_only.validate(
        resp=Response(HTTP_200=ExampleModel),
        publish=True,
        category="widgets",
        tags=["widgets"],
    )
    def get(self, widget_id):
        pass


def test_class_view_path_with_typed_converter_is_published():
    """A class-based (MethodView) route whose path uses a typed Werkzeug
    converter (e.g. ``<uuid:widget_id>``) must still be published when
    ``publish=True``, in "publish_only" mode.

    register_class_view_apidoc() used to key ``class_view_apispec`` via a
    naive "<" -> "{" / ">" -> "}" string replacement on the raw rule, which
    left the converter prefix in place ("<uuid:widget_id>" ->
    "{uuid:widget_id}"). _generate_spec() looks the same route up via
    parse_path(), which correctly strips the converter ("{widget_id}"). The
    keys never matched for any typed converter, so the route silently fell
    through to the "not a class view" branch and was treated as unpublished
    regardless of its real `publish` value.
    """
    app = Flask(__name__)
    view = WidgetView.as_view("WidgetView")
    app.add_url_rule("/widgets/<uuid:widget_id>", view_func=view)
    api_publish_only.register(app)
    api_publish_only.register_class_view_apidoc(WidgetView)

    assert get_paths(api_publish_only.spec) == ["/widgets/{widget_id}"]
    assert list(api_publish_only.spec["paths"]["/widgets/{widget_id}"].keys()) == [
        "get"
    ]


api_publish_only_gadgets = FlaskPydanticSpec("flask", mode="publish_only")


class UnpublishedGadgetView(MethodView):
    # func.__qualname__ must resolve to just "UnpublishedGadgetView.get" for
    # class_view_api_info's decoration-time bookkeeping to key correctly --
    # defining this class inside the test function (nested scope) breaks
    # that (qualname becomes "test_name.<locals>.UnpublishedGadgetView.get",
    # so view_name, *_, method = qualname.split(".") picks up the *test
    # function's* name instead), same pitfall as WidgetView above.
    @api_publish_only_gadgets.validate(
        resp=Response(HTTP_200=ExampleModel),
        publish=False,
        category="gadgets",
        tags=["gadgets"],
    )
    def get(self, gadget_id):
        pass


def test_spec_by_category_with_zero_published_routes_does_not_raise():
    """A category where every route is publish=False never gets a
    routes_by_category entry (_generate_spec() only creates one once it
    processes a route belonging to that category) -- spec_by_category()
    used to index into routes_by_category with a plain `[category]`,
    raising a raw KeyError instead of returning a valid, empty document.
    """
    app = Flask(__name__)
    view = UnpublishedGadgetView.as_view("UnpublishedGadgetView")
    app.add_url_rule("/gadgets/<uuid:gadget_id>", view_func=view)
    api_publish_only_gadgets.register(app)
    api_publish_only_gadgets.register_class_view_apidoc(UnpublishedGadgetView)

    spec = api_publish_only_gadgets.spec_by_category("gadgets")
    assert spec["paths"] == {}
