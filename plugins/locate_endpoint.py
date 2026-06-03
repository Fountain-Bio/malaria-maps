"""Datasette plugin: GET /-/locate?q=<city> (or ?geonameId=<id>).

Geocodes a place via GeoNames (server-side, username from .env), maps it to our country
by ISO2, and returns the residence/travel deferral verdict from malaria_tracker.locate.
The SRTM elevation call is made only when the country's rules need it, and an elevation
failure degrades to "unknown" rather than failing the whole lookup.
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
            cand = await asyncio.to_thread(geocode.lookup_geoname, int(gid))
            alternates = []
        else:
            cands = await asyncio.to_thread(geocode.search_place, q)
            cand = cands[0] if cands else None
            alternates = cands[1:5] if cands else []
    except geocode.GeoNamesError as exc:
        return Response.json({"error": f"geocoding failed: {exc}"}, status=502)
    except ValueError:
        return Response.json({"error": "geonameId must be an integer"}, status=400)

    label = q or (cand.name if cand else gid)
    if cand is None:
        return Response.json({"query": label, "error": "no geocode match"}, status=404)

    db = datasette.get_database("malaria")
    rec_rows = list(await db.execute(locate.RECORD_SQL, [cand.country_iso2]))
    record = dict(rec_rows[0]) if rec_rows else None
    areas = []
    if record:
        area_rows = await db.execute(locate.AREA_SQL, [record["record_id"]])
        areas = [dict(r) for r in area_rows]

    elevation = None
    if locate.needs_elevation(areas):
        try:
            elevation = await asyncio.to_thread(geocode.fetch_elevation, cand.lat, cand.lng)
        except geocode.GeoNamesError:
            elevation = None      # degrade: travel becomes "uncertain" on elevation-gated rules

    geo = {**cand.to_dict(), "elevation_m": elevation}
    verdict = locate.determine(label, geo, record, areas)
    verdict.alternates = [c.to_dict() for c in alternates]
    return Response.json(verdict.to_dict())
