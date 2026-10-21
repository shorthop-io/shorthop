"""M5 API: noise, response times and correlation over the alert history."""
from flask_smorest import Blueprint
from marshmallow import EXCLUDE, Schema, fields, validate

from .. import insight
from ..auth import require

blp = Blueprint("insight", __name__, url_prefix="/api/v1",
                description="What the history says: noisy alerts, response times, correlation")


class _In(Schema):
    class Meta:
        unknown = EXCLUDE


class WindowArgs(_In):
    days = fields.Int(load_default=30, validate=validate.Range(min=1, max=90))


class RelatedArgs(WindowArgs):
    signature = fields.Str(required=True, validate=validate.Length(min=1, max=64))


@blp.route("/insight")
@require("alerts:read")
@blp.arguments(WindowArgs, location="query")
def insight_report(args):
    """Noisiest alerts with flags (flapping / nobody acts / short-lived), MTTA and MTTR as
    percentiles overall and per team, delivery counts, and alerts that fire together."""
    return insight.report(args["days"])


@blp.route("/insight/related")
@require("alerts:read")
@blp.arguments(RelatedArgs, location="query")
def insight_related(args):
    """Alerts that start within five minutes of this one, most consistently first."""
    return insight.related(args["signature"], args["days"])
