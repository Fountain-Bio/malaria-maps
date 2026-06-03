"""Datasette plugin: GET /-/locate?q=<city> (or ?geonameId=<id>).

Geocodes a place via GeoNames (server-side, username from .env), maps it to our country
by ISO2, and returns the residence/travel deferral verdict from malaria_tracker.locate.
Keeps the GeoNames username off the client and the determination logic in tested Python.
"""

import asyncio

from datasette import hookimpl
from datasette.utils.asgi import Response

from malaria_tracker import geocode, locate


@hookimpl
def register_routes():
    return [(r"^/-/locate$", locate_view)]


async def locate_view(request, datasette):
    q = request.args.get("q")
    gid = request.args.get("geonameId")
    if not q and not gid:
        return Response.json({"error": "provide ?q=<city> or ?geonameId=<id>"}, status=400)

    try:
        if gid:
            result = await asyncio.to_thread(geocode.resolve_geoname_id, int(gid))
        else:
            result = await asyncio.to_thread(geocode.geocode, q)
    except geocode.GeoNamesError as exc:
        return Response.json({"error": f"geocoding failed: {exc}"}, status=502)
    except ValueError:
        return Response.json({"error": "geonameId must be an integer"}, status=400)

    label = q or (result.chosen.name if result else gid)
    if result is None:
        return Response.json({"query": label, "error": "no geocode match"}, status=404)

    geo = {**result.chosen.to_dict(), "elevation_m": result.elevation_m}
    db = datasette.get_database("malaria")
    rec_rows = list(await db.execute(locate.RECORD_SQL, [result.chosen.country_iso2]))
    record = dict(rec_rows[0]) if rec_rows else None
    areas = []
    if record:
        area_rows = await db.execute(locate.AREA_SQL, [record["record_id"]])
        areas = [dict(r) for r in area_rows]

    verdict = locate.determine(label, geo, record, areas)
    locate.attach_record_fields(verdict, record)
    verdict.alternates = [c.to_dict() for c in result.alternates]
    return Response.json(verdict.to_dict())
