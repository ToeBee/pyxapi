from flask import Flask, Response, request
import psycopg2
import psycopg2.extras
import re
import itertools

app = Flask(__name__)
db = psycopg2.connect(host='localhost', dbname='xapi', user='xapi', password='xapi')
psycopg2.extras.register_hstore(db)

def stream_osm_data(cursor):
    """Streams OSM data from psql temp tables."""
    yield '<?xml version="1.0" encoding="UTF-8"?>\n'
    yield '<osm version="0.6" generator="pyxapi" copyright="OpenStreetMap and contributors" attribution="http://www.openstreetmap.org/copyright" license="http://opendatacommons.org/licenses/odbl/1-0/">\n'

    cursor.execute("SELECT id, version, changeset_id, ST_X(geom) as longitude, ST_Y(geom) as latitude, user_id, tstamp, tags FROM bbox_nodes ORDER BY id")

    for row in cursor:
        tags = row.get('tags', {})
        yield '<node id="{id}" version="{version}" changeset="{changeset_id}" lat="{latitude}" lon="{longitude}" uid="{user_id}" visible="true" timestamp="{timestamp}"'.format(timestamp=row.get('tstamp').isoformat(), **row)

        if tags:
            yield '>\n'

            for tag in tags.iteritems():
                yield '<tag k="{}" v="{}" />\n'.format(*tag)

            yield "</node>\n"
        else:
            yield '/>\n'


    cursor.execute("SELECT * FROM bbox_ways ORDER BY id")

    for row in cursor:
        tags = row.get('tags', {})
        nds = row.get('nodes', [])
        yield '<way id="{id}" version="{version}" changeset="{changeset_id}" uid="{user_id}" visible="true" timestamp="{timestamp}"'.format(timestamp=row.get('tstamp').isoformat(), **row)

        if tags or nds:
            yield '>\n'

            for tag in tags.iteritems():
                yield '<tag k="{}" v="{}" />\n'.format(*tag)

            for nd in nds:
                yield '<nd ref="{}" />\n'.format(nd)

            yield "</way>\n"
        else:
            yield '/>\n'

    cursor.execute("SELECT * FROM bbox_relations ORDER BY id")

    relation_cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
    for row in cursor:
        tags = row.get('tags', {})
        relation_cursor.execute("""SELECT relation_id AS entity_id, member_id, member_type, member_role, sequence_id
                                   FROM relation_members f
                                   WHERE relation_id=%s
                                   ORDER BY sequence_id""", (row.get('id'),))
        print relation_cursor.query

        yield '<relation id="{id}" version="{version}" changeset="{changeset_id}" uid="{user_id}" visible="true" timestamp="{timestamp}"'.format(timestamp=row.get('tstamp').isoformat(), **row)

        if tags or relation_cursor.rowcount > 0:
            yield '>\n'

            for tag in tags.iteritems():
                yield '<tag k="{}" v="{}" />\n'.format(*tag)

            for member in relation_cursor:
                member_type = member.get('member_type', None)
                if member_type == 'N':
                    member_type = 'node'
                elif member_type == 'W':
                    member_type = 'way'
                elif member_type == 'R':
                    member_type = 'relation'
                member['member_type'] = member_type

                yield '<member role="{member_role}" type="{member_type}" id="{member_id}" />\n'.format(**member)

            yield "</relation>\n"
        else:
            yield '/>\n'


    yield '</osm>\n'

    # Remove the temp tables
    cursor.connection.rollback()

def query_nodes(cursor, where_str, where_obj=None):
    cursor.execute("""CREATE TEMPORARY TABLE bbox_nodes ON COMMIT DROP AS
                        SELECT *
                        FROM nodes
                        WHERE %s""" % where_str, where_obj)
    print cursor.query

def query_ways(cursor, where_str, where_obj=None):
    cursor.execute("""CREATE TEMPORARY TABLE bbox_ways ON COMMIT DROP AS
                        SELECT *
                        FROM ways
                        WHERE %s""" % where_str, where_obj)
    print cursor.query

def query_relations(cursor, where_str, where_obj=None):
    cursor.execute("""CREATE TEMPORARY TABLE bbox_relations ON COMMIT DROP AS
                        SELECT *
                        FROM relations
                        WHERE %s""" % where_str, where_obj)
    print cursor.query

def backfill_way_nodes(cursor):
    cursor.execute("""CREATE TEMPORARY TABLE bbox_way_nodes (id bigint) ON COMMIT DROP""")
    cursor.execute("""SELECT unnest_bbox_way_nodes()""")
    cursor.execute("""CREATE TEMPORARY TABLE bbox_missing_way_nodes ON COMMIT DROP AS
                SELECT buwn.id FROM (SELECT DISTINCT bwn.id FROM bbox_way_nodes bwn) buwn
                WHERE NOT EXISTS (
                    SELECT * FROM bbox_nodes WHERE id = buwn.id
                );""")
    cursor.execute("""ALTER TABLE ONLY bbox_missing_way_nodes
                ADD CONSTRAINT pk_bbox_missing_way_nodes PRIMARY KEY (id)""")
    cursor.execute("""ANALYZE bbox_missing_way_nodes""")
    cursor.execute("""INSERT INTO bbox_nodes
                SELECT n.* FROM nodes n INNER JOIN bbox_missing_way_nodes bwn ON n.id = bwn.id;""")

def backfill_relations(cursor):
    cursor.execute("""CREATE TEMPORARY TABLE bbox_relations ON COMMIT DROP AS
                     SELECT r.* FROM relations r
                     INNER JOIN (
                        SELECT relation_id FROM (
                            SELECT rm.relation_id AS relation_id FROM relation_members rm
                            INNER JOIN bbox_nodes n ON rm.member_id = n.id WHERE rm.member_type = 'N'
                            UNION
                            SELECT rm.relation_id AS relation_id FROM relation_members rm
                            INNER JOIN bbox_ways w ON rm.member_id = w.id WHERE rm.member_type = 'W'
                         ) rids GROUP BY relation_id
                    ) rids ON r.id = rids.relation_id""")
    print cursor.query

def backfill_parent_relations(cursor):
    while True:
        rows = cursor.execute("""INSERT INTO bbox_relations
                    SELECT r.* FROM relations r INNER JOIN (
                        SELECT rm.relation_id FROM relation_members rm
                        INNER JOIN bbox_relations br ON rm.member_id = br.id
                        WHERE rm.member_type = 'R' AND NOT EXISTS (
                            SELECT * FROM bbox_relations br2 WHERE rm.relation_id = br2.id
                        ) GROUP BY rm.relation_id
                    ) rids ON r.id = rids.relation_id""")
        print cursor.query
        if cursor.rowcount == 0:
            break

class QueryError(Exception):
    pass

def parse_xapi(predicate):
    query = []
    query_objs = []
    groups = re.findall(r'(?:\[(.*?)\])', predicate)
    for g in groups:
        (left, right) = g.split('=')
        if left == '@uid':
            query.append('uid = %s')
            query_objs.append(int(right))
        elif left == '@changeset':
            query.append('changeset_id = %s')
            query_objs.append(int(right))
        elif left == 'bbox':
            try:
                (left, bottom, right, top) = tuple(float(v) for v in right.split(','))
            except ValueError, e:
                raise QueryError('Invalid bbox.')

            if left > right:
                raise QueryError('Left > Right.')
            if bottom > top:
                raise QueryError('Bottom > Top.')
            if bottom < -90 or bottom > 90:
                raise QueryError('Bottom is out of range.')
            if top < -90 or top > 90:
                raise QueryError('Top is out of range.')
            if left < -180 or left > 180:
                raise QueryError('Left is out of range.')
            if right < -180 or right > 180:
                raise QueryError('Right is out of range.')

            (left, bottom, right, top) = tuple(float(v) for v in right.split(','))
            query.append('ST_Intersects(geom, ST_GeometryFromText(\'POLYGON((%s %s, %s %s, %s %s, %s %s, %s %s))\', 4326))')
            query_objs.extend([left, bottom, left, top, right, top, right, bottom, left, bottom])
        else:
            ors = []
            orvs = []
            keys = left.split('|')
            vals = right.split('|')
            for (l,r) in itertools.product(keys, vals):
                if r == '*':
                    ors.append('(tags ? %s)')
                    orvs.append(l)
                else:
                    ors.append('(tags @> hstore(%s, %s))')
                    orvs.append(l)
                    orvs.append(r)
            query.append('(' + ' OR '.join(ors) + ')')
            query_objs.extend(orvs)
    query_str = ' AND '.join(query)
    return (query_str, query_objs)

@app.route("/api/capabilities")
def capabilities():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="pyxapi" copyright="OpenStreetMap and contributors" attribution="http://www.openstreetmap.org/copyright" license="http://opendatacommons.org/licenses/odbl/1-0/">
  <api>
    <version minimum="0.6" maximum="0.6"/>
    <area maximum="0.25"/>
    <timeout seconds="300"/>
  </api>
</osm>"""
    return Response(xml, mimetype='text/xml')

@app.route("/api/0.6/node/<string:ids>")
def nodes(ids):
    ids = ids.split(',')

    cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)

    query_nodes(cursor, 'id IN %s', (tuple(ids),))

    if cursor.rowcount < 1:
        return Response('Node %s not found.' % ids, status=404)

    query_ways(cursor, 'FALSE')

    query_relations(cursor, 'FALSE')

    return Response(stream_osm_data(cursor), mimetype='text/xml')

@app.route("/api/0.6/way/<string:ids>")
def ways(ids):
    ids = ids.split(',')

    cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)

    query_nodes(cursor, 'FALSE')

    query_ways(cursor, 'id IN %s', (tuple(ids),))

    if cursor.rowcount < 1:
        return Response('Way %s not found.' % ids, status=404)

    cursor.execute("""ANALYZE bbox_ways""")

    backfill_way_nodes(cursor)

    cursor.execute("""ANALYZE bbox_nodes""")

    query_relations(cursor, 'FALSE')

    return Response(stream_osm_data(cursor), mimetype='text/xml')

@app.route("/api/0.6/relation/<string:ids>")
def relations(ids):
    ids = ids.split(',')

    cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)

    query_nodes(cursor, 'FALSE')

    query_ways(cursor, 'FALSE')

    query_relations(cursor, 'id IN %s', (tuple(ids),))

    if cursor.rowcount < 1:
        return Response('Relation %s not found.' % ids, status=404)

    return Response(stream_osm_data(cursor), mimetype='text/xml')

@app.route('/api/0.6/map')
def map():
    bbox = request.args.get('bbox')

    try:
        (query_str, query_objs) = parse_xapi('[bbox=%s]' % bbox)
    except QueryError, e:
        return Response(e.message, status=400)

    cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)

    query_nodes(cursor, 'ST_Intersects(geom, ST_GeometryFromText(\'POLYGON((%s %s, %s %s, %s %s, %s %s, %s %s))\', 4326))', (left, bottom, left, top, right, top, right, bottom, left, bottom))
    cursor.execute("""ALTER TABLE ONLY bbox_nodes ADD CONSTRAINT pk_bbox_nodes PRIMARY KEY (id)""")

    query_ways(cursor, 'ST_Intersects(linestring, ST_GeometryFromText(\'POLYGON((%s %s, %s %s, %s %s, %s %s, %s %s))\', 4326))', (left, bottom, left, top, right, top, right, bottom, left, bottom))
    cursor.execute("""ALTER TABLE ONLY bbox_ways ADD CONSTRAINT pk_bbox_ways PRIMARY KEY (id)""")

    backfill_relations(cursor)
    backfill_parent_relations(cursor)
    backfill_way_nodes(cursor)

    cursor.execute("""ANALYZE bbox_nodes""")
    cursor.execute("""ANALYZE bbox_ways""")
    cursor.execute("""ANALYZE bbox_relations""")

    return Response(stream_osm_data(cursor), mimetype='text/xml')

@app.route('/api/0.6/node<string:predicate>')
def search_nodes(predicate):
    try:
        (query_str, query_objs) = parse_xapi(predicate)
    except QueryError, e:
        return Response(e.message, status=400)

    cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)

    query_nodes(cursor, query_str, query_objs)

    query_ways(cursor, 'FALSE')

    query_relations(cursor, 'FALSE')

    return Response(stream_osm_data(cursor), mimetype='text/xml')

@app.route('/api/0.6/way<string:predicate>')
def search_ways(predicate):
    try:
        (query_str, query_objs) = parse_xapi(predicate)
    except QueryError, e:
        return Response(e.message, status=400)

    cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)

    query_nodes(cursor, 'FALSE')

    query_ways(cursor, query_str.replace('geom', 'linestring'), query_objs)
    backfill_way_nodes(cursor)

    query_relations(cursor, 'FALSE')

    return Response(stream_osm_data(cursor), mimetype='text/xml')

@app.route('/api/0.6/relation<string:predicate>')
def search_relations(predicate):
    try:
        (query_str, query_objs) = parse_xapi(predicate)
    except QueryError, e:
        return Response(e.message, status=400)

    cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)

    return Response(stream_osm_data(cursor), mimetype='text/xml')

@app.route('/api/0.6/*<string:predicate>')
def search_primitives(predicate):
    try:
        (query_str, query_objs) = parse_xapi(predicate)
    except QueryError, e:
        return Response(e.message, status=400)

    cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)

    query_nodes(cursor, query_str, query_objs)

    query_ways(cursor, query_str.replace('geom', 'linestring'), query_objs)
    backfill_way_nodes(cursor)

    query_relations(cursor, 'FALSE')

    return Response(stream_osm_data(cursor), mimetype='text/xml')

if __name__ == "__main__":
    app.run(debug=True)