import psycopg2
import copy
import cPickle
import re
from operator import itemgetter
from django.conf import settings
from protocol import expertsrc_pb2
from protocol.batchqueue import BatchQueue
import logging
import random

logger = logging.getLogger(__name__)

# convert float (0 to 1) to 8 bit web color (e.g. 00 to ff)
def f2c(x):
    if x > 1.0: x = 1.0
    if x < 0.0: x = 0.0
    c = hex(int(255*x))[2:]
    if len(c) == 1:
        c = '0' + c
    return c

# Faster than copy.deepcopy, but totally hacky:
# http://stackoverflow.com/questions/1410615/copy-deepcopy-vs-pickle
def copyhack(obj):
    return cPickle.loads(cPickle.dumps(obj, -1))


class TamerDB:
    conn = None
    name = None

    def __init__(self, dbname):
        if self.conn is None:
            self.conn = psycopg2.connect(database=dbname,
                                         user=settings.DATABASES['default']['USER'],
                                         password=settings.DATABASES['default']['PASSWORD'],
                                         host=settings.DATABASES['default']['HOST'])
        name = dbname

    def source_list(self, n):
        cur = self.conn.cursor()
        cmd = '''SELECT id, local_id FROM local_sources LIMIT %s'''
        cur.execute(cmd, (n,))
        sl = []
        for r in cur.fetchall():
            sl.append({'id': r[0], 'name': r[1]})
        return sl

    def recent_sources(self, n):
        cur = self.conn.cursor()
        cmd = '''SELECT COUNT(*), date_added,
                        row_number() OVER (ORDER BY date_added) rank
                   FROM local_sources
               GROUP BY date_added
                  LIMIT %s;'''
        cur.execute(cmd, (n,))
        return [{'date': r[1], 'count': r[0], 'rank': r[2]} for r in cur.fetchall()]

    def schema_tables(self, schemaname):
        cur = self.conn.cursor()
        cmd = '''SELECT tablename FROM pg_tables
                  WHERE schemaname = %s ORDER BY tablename;'''
        cur.execute(cmd, (schemaname,))
        t = []
        for r in cur.fetchall():
            t.append(r[0])
        return t

    def table_attributes(self, tablename):
        cur = self.conn.cursor()
        cmd = '''SELECT attname FROM pg_attribute, pg_type
                  WHERE typname = %s
                    AND attrelid = typrelid
                    AND attname NOT IN ('cmin', 'cmax', 'ctid', 'oid', 'tableoid', 'xmin', 'xmax');'''
        cur.execute(cmd, (tablename,))
        a = []
        for r in cur.fetchall():
            a.append(r[0])
        return a

    def global_attributes(self):
        cur = self.conn.cursor()
        cmd = '''SELECT id, name FROM global_attributes;'''
        cur.execute(cmd)
        return [{'id': r[0], 'name': r[1]} for r in cur.fetchall()]

    def global_attribute_names(self):
        cur = self.conn.cursor()
        cmd = '''SELECT name FROM global_attributes;'''
        cur.execute(cmd)
        return [r[0] for r in cur.fetchall()]

    def source_name(self, sid):
        cur = self.conn.cursor()
        cmd = '''SELECT local_id FROM local_sources WHERE id = %s;'''
        cur.execute(cmd, (sid,))
        return cur.fetchone()[0]

    def source_stats(self, sid):
        cur = self.conn.cursor()
        stats = {}
        cmd = '''SELECT COUNT(*) FROM local_entities WHERE source_id = %s;'''
        cur.execute(cmd, (sid,))
        stats['nent'] = cur.fetchone()[0]

        cmd = '''SELECT COUNT(*), COUNT(a.local_id)
                   FROM local_fields f
              LEFT JOIN attribute_mappings a
                     ON f.id = a.local_id
                  WHERE source_id = %s;'''
        cur.execute(cmd, (sid,))
        r = cur.fetchone()
        stats['ncol'] = r[0]
        stats['nmap'] = r[1]

        cmd = '''SELECT COUNT(*) FROM entity_matches
                  WHERE entity_a IN (SELECT id FROM local_entities WHERE source_id = %s);'''
        cur.execute(cmd, (sid,))
        stats['ndup'] = cur.fetchone()[0]
        return stats

    def config_params(self, model_name):
        cur = self.conn.cursor()
        cmd = '''SELECT name, COALESCE(description, name), value FROM configuration_properties
                 WHERE module = %s;'''
        cur.execute(cmd, (model_name,))
        return [{'name': r[0], 'description': r[1], 'value': r[2]} for r in cur.fetchall()]

    def set_config(self, param_name, param_value):
        cur = self.conn.cursor()
        cmd = '''UPDATE configuration_properties SET value = %s
                  WHERE name = %s;'''
        cur.execute(cmd, (param_value, param_name,))
        self.conn.commit()
        return cmd % (param_name, param_value)

    def dedup_model_exists(self):
        cur = self.conn.cursor()
        # #cmd = '''SELECT COUNT(*) FROM learning_attrs;'''
        # cmd = '''SELECT COUNT(weight), COUNT(*) FROM entity_field_weights;'''
        # cur.execute(cmd)
        # r = cur.fetchone()
        # return (int(r[0]) == int(r[1]) and int(r[0]) > 0)
        return False

    ##
    # Major jobs
    ##
    def import_from_pg_table(self, schemaname, tablename, eidattr, sidattr, dataattr):
        cur = self.conn.cursor()

        # Make a copy for importing
        eidconst = 'row_number() over ()' if eidattr is None else eidattr
        sidconst = "'0'" if sidattr is None else sidattr
        cmd = '''CREATE TEMP TABLE import_tmp AS
                      SELECT %s::TEXT AS sid, %s::TEXT AS eid, %s::TEXT FROM %s.%s''' \
            % (sidconst, eidconst, '::TEXT,'.join(dataattr), schemaname, tablename)
        cur.execute(cmd)

        # Add new source(s)
        cmd = '''INSERT INTO local_sources (local_id, date_added)
                      SELECT DISTINCT %s || '/' || sid, NOW() FROM import_tmp;'''
        cur.execute(cmd, (tablename,))

        # Get new source_id(s)
        cmd = '''UPDATE import_tmp i SET sid = s.id FROM local_sources s
                  WHERE s.local_id = %s || '/' || i.sid;'''
        cur.execute(cmd, (tablename,))

        # Add data columns to local_fields
        cmd = '''INSERT INTO local_fields (source_id, local_name)
                      SELECT sid::INTEGER, %s FROM import_tmp GROUP BY sid;'''
        for a in dataattr:
            cur.execute(cmd, (a,))

        # Add entities to local_entities
        cmd = '''INSERT INTO local_entities (source_id, local_id)
                      SELECT sid::INTEGER, eid
                        FROM import_tmp
                    GROUP BY sid, eid;
                 UPDATE import_tmp i SET eid = e.id FROM local_entities e
                  WHERE i.eid = e.local_id;'''
        cur.execute(cmd)

        # Add data to local_data
        for a in dataattr:
            cmd = '''INSERT INTO local_data (field_id, entity_id, value)
                          SELECT f.id, eid::INTEGER, i.%s
                            FROM import_tmp i, local_fields f
                           WHERE i.sid::INTEGER = f.source_id
                             AND f.local_name = %%s
                             AND i.%s IS NOT NULL
                             AND length(i.%s) > 0;''' \
                % (a, a, a)
            cur.execute(cmd, (a,))

        cmd = '''SELECT DISTINCT sid::INTEGER FROM import_tmp;'''
        cur.execute(cmd)
        for r in cur.fetchall():
            logger.info('sid -> %s', r[0])
            cmd = '''INSERT INTO local_source_meta (source_id, meta_name, value)
                     VALUES (%s, 'expertsrc_domain', 'data-tamer')'''
            cur.execute(cmd, (r[0],))

        # Preprocess source(s) for map and dedup
        cmd = '''SELECT DISTINCT sid::INTEGER FROM import_tmp;'''
        cur.execute(cmd)
        for r in cur.fetchall():
            self.preprocess_source(r[0])

        self.conn.commit()

    def import_attribute_dictionary(self, att_id, schemaname, tablename, columnname):
        cur = self.conn.cursor()
        cmd = '''INSERT INTO global_data (att_id, value)
                      SELECT %%s, %s::TEXT FROM %s.%s;''' \
            % (columnname, schemaname, tablename)
        cur.execute(cmd, (att_id,))
        self.conn.commit()

    def import_synonym_dictionary(self, att_id, schemaname, tablename, columna, columnb):
        cur = self.conn.cursor()
        cmd = '''INSERT INTO global_synonyms (att_id, value_a, value_b)
                      SELECT %%s, %s::TEXT, %s::TEXT FROM %s.%s;''' \
            % (columna, columnb, schemaname, tablename)
        cur.execute(cmd, (att_id,))
        self.conn.commit()

    def import_attribute_template(self, templatename, schemaname, tablename, columnname):
        cur = self.conn.cursor()
        cmd = '''INSERT INTO templates (name) SELECT %s;'''
        cur.execute(cmd, (templatename,))
        cmd = '''SELECT id FROM templates WHERE name = %s;;'''
        cur.execute(cmd, (templatename,))
        tid = cur.fetchone()[0]
        cmd = '''INSERT INTO attribute_templates (template_id, att_id)
                      SELECT %%s, g.id
                        FROM %s.%s t, global_attributes g
                       WHERE lower(t.%s::TEXT) = lower(g.name);''' \
            % (schemaname, tablename, columnname)
        cur.execute(cmd, (tid,))
        self.conn.commit()

    def import_global_schema(self, schemaname, tablename, columnname):
        cur = self.conn.cursor()
        cmd = '''INSERT INTO global_attributes (name, derived_from)
                      SELECT %s, 'WEB' FROM %s.%s;''' \
            % (columnname, schemaname, tablename)
        cur.execute(cmd)
        self.conn.commit()

    def preprocess_source(self, sid):
        cur = self.conn.cursor()
        cmd = '''SELECT preprocess_source(%s); '''
                 # --SELECT extract_new_data(%s, true);
                 # TRUNCATE entity_test_group;
                 # INSERT INTO entity_test_group
                 #      SELECT id FROM local_entities WHERE source_id = %s;
                 # SELECT entities_preprocess_test_group('t');'''
        cur.execute(cmd, (sid,))
        self.conn.commit()

    def init_dedup(self, important, irrelevant):
        cur = self.conn.cursor()
        # #cmd = '''INSERT INTO learning_attrs (tag_id)
        # #              SELECT id
        # #                FROM global_attributes
        # #               WHERE name = %s;'''
        # cmd = '''TRUNCATE entity_field_weights;
        #          INSERT INTO entity_field_weights
        #               SELECT id, 1.0 FROM global_attributes;'''
        # cur.execute(cmd)
        # cmd = '''UPDATE entity_field_weights SET initial_bias = %s
        #           WHERE field_id IN (SELECT id FROM global_attributes WHERE name = %s);'''
        # for attr in important:
        #     cur.execute(cmd, (10.0, attr))
        # for attr in irrelevant:
        #     cur.execute(cmd, (0.1, attr))
        # cmd = '''UPDATE entity_field_weights SET weight = initial_bias;'''
        # cur.execute(cmd)
        # self.conn.commit()

    def rebuild_dedup_models(self):
        cur = self.conn.cursor()
        # cmd = '''--SELECT learn_weights(0.05, 0.00001, 5, 1000, 0.2);
        #          TRUNCATE entity_test_group;
        #          INSERT INTO entity_test_group SELECT id FROM local_entities;
        #          SELECT entities_preprocess_test_group('t');
        #          SELECT entities_weights_from_test_group();'''
        # cur.execute(cmd)
        # self.conn.commit()

    def rebuild_schema_mapping_models(self):
        cur = self.conn.cursor()
        cmd = '''SELECT preprocess_global();'''
        cur.execute(cmd)
        self.conn.commit()

    def dictfetchall(self, cur):
        desc = cur.description
        return [
            dict(zip([col[0] for col in desc], row))
            for row in cur.fetchall()
            ]

    def schema_map_source(self, sourceid):
        cur = self.conn.cursor()
        cmd = '''SELECT qgrams_results_for_source(%s);
                 SELECT ngrams_results_for_source(%s);
                 SELECT mdl_results_for_source(%s);
                 SELECT nr_composite_load();'''
        cur.execute(cmd, (sourceid, sourceid, sourceid))
        self.conn.commit()

    def answer_with_thresh(self, sourceid, thresh):
        cur = self.conn.cursor()
        mappings = self.get_field_mappings_by_source(sourceid)
        to_map = []
        for fid in mappings:
            attr = mappings[fid]
            if 'who_mapped' not in attr['match'] and float(attr['match']['score']) >= float(thresh):
                to_map.append({'local_id': fid, 'global_id': attr['match']['id']})
        cmd = """ INSERT INTO attribute_mappings
                    (local_id, global_id, when_created, who_created, confidence, authority, why_created)
                  VALUES (%(local_id)s, %(global_id)s, NOW(), -2, 1, 1, 'AUTO') """
        cur.executemany(cmd, to_map)
        self.conn.commit()

    def register_schema_map(self, sourceid):
        cur = self.conn.cursor()
        mappings = self.get_field_mappings_by_source(sourceid, only_unmapped=True)
        cmd = "SELECT id, name FROM global_attributes"
        cur.execute(cmd)
        global_attributes = self.dictfetchall(cur)
        batch_obj = expertsrc_pb2.QuestionBatch()
        batch_obj.type = expertsrc_pb2.QuestionBatch.SCHEMAMAP
        # TODO: make sure to grab this from auth service using provided cookie
        batch_obj.asker_name = 'data-tamer'
        cmd = """SELECT local_id, value
                 FROM local_sources ls, local_source_meta lsm
                 WHERE ls.id = %s and lsm.source_id = ls.id and
                       lsm.meta_name = 'expertsrc_domain'"""
        cur.execute(cmd, (sourceid,))
        source_name, domain_name = cur.fetchone()
        logger.info('source_name -> %s' % source_name)
        logger.info('domain_name -> %s' % domain_name)
        batch_obj.source_name = source_name
        batch = BatchQueue('question', batch_obj)
        for fid in mappings.keys():
            question = batch.getbatchobj().question.add()
            question.domain_name = domain_name
            question.local_field_id = fid
            question.local_field_name = mappings[fid]['name']
            choices = mappings[fid]['matches']
            ids = list()
            choice_count = 10
            for c in choices:
                if choice_count > 0:
                    choice = question.choice.add()
                    choice.global_attribute_id = c['id']
                    choice.global_attribute_name = c['name']
                    choice.confidence_score = c['score']
                    ids.append(c['id'])
                    choice_count -= 1
            # uncomment this if you want to add all global attributes as
            # potential choices.
            id_set = set(ids)
            for a in global_attributes:
                if a['id'] not in id_set:
                    choice = question.choice.add()
                    choice.global_attribute_id = a['id']
                    choice.global_attribute_name = a['name']
        batch.enqueue()
        self.conn.commit()

    def get_field_mappings_by_source(self, source_id, only_unmapped=False):
        """ Retrieves all possible mappings for all fields in a source."""
        cur = self.conn.cursor()
        fields = dict()
        cmd = '''SELECT lf.id, lf.local_name, ama.global_id, ga.name,
                        ama.who_created
                   FROM local_fields lf
              LEFT JOIN attribute_mappings ama
                     ON lf.id = ama.local_id
              LEFT JOIN global_attributes ga
                     ON ama.global_id = ga.id
                  WHERE lf.source_id = %s'''
        cur.execute(cmd, (source_id,))

        for rec in cur.fetchall():
            fid, fname, gid, gname, who = rec
            fields.setdefault(fid, {'id': fid, 'name': fname})
            if gid is not None:
                fields[fid]['match'] = {
                    'id': gid, 'name': gname, 'who_mapped': who,
                    'is_mapping': True, 'score': 2.0}

        cmd = '''SELECT lf.id, lf.local_name, nnr.match_id, ga.name, nnr.score
                   FROM nr_ncomp_results_tbl nnr, global_attributes ga,
                        local_fields lf
                  WHERE nnr.field_id = lf.id
                    AND nnr.source_id = %s
                    AND nnr.match_id = ga.id
                    AND (lf.n_values > 0 OR 1 = 1)
               ORDER BY score desc;'''

        cur.execute(cmd, (source_id,))
        records = cur.fetchall()

        for rec in records:
            fid, fname, gid, gname, score = rec
            fields[fid].setdefault('match', {
                'id': gid, 'name': gname, 'score': score,
                'green': f2c(score / 1.0), 'red':f2c(1.0 - score / 2.0)})

            matches = fields[fid].setdefault('matches', list())
            matches.append({'id':gid, 'name':gname, 'score':score,
                            'green': f2c(score / 1.0), 'red':f2c(1.0 - score / 2.0)})

        for fid in fields:
            if 'match' not in fields[fid]:
                fields[fid]['match'] = {'id': 0, 'name': 'Unknown', 'score': 0, 'green': f2c(0), 'red': f2c(1)}
                fields[fid]['matches'] = list()

        to_del = []
        if only_unmapped:
            for fid in fields:
                if 'who_mapped' in fields[fid]['match']:
                    to_del.append(fid)
            for fid in to_del:
                del fields[fid]

        return fields

    def dedup_source(self, sid):
        cur = self.conn.cursor()
        cmd = '''select cat_entity_source(%s);
                 select add_source(%s);'''
        cur.execute(cmd, (sid, sid,))
        self.conn.commit()


    def dedup_all(self):
        cur = self.conn.cursor()
        # self.rebuild_dedup_models()
        # cmd = '''TRUNCATE entity_test_group;
        #          INSERT INTO entity_test_group
        #               SELECT id FROM local_entities;
        #          SELECT entities_preprocess_test_group('t');
        #          SELECT entities_field_similarities_for_test_group();
        #          SELECT entities_results_for_test_group('f');'''
        # cur.execute(cmd)
        # self.conn.commit()

    # get two entites to compare
    def get_entities_to_compare(self, approx_sim, sort):
        cur = self.conn.cursor()
        cmd = '''SELECT entity_a, entity_b, similarity
                   FROM entity_similarities
               ORDER BY random()
                  LIMIT 1;'''
        if sort == 'high':
            cmd = '''SELECT entity_a, entity_b, similarity FROM entity_similarities
                      WHERE human_label IS NULL ORDER BY similarity DESC;'''
        if approx_sim is not None:
            cmd = '''SELECT entity_a, entity_b, similarity
                       FROM entity_similarities
                      WHERE similarity BETWEEN %s - 0.05 AND %s + 0.05
                   ORDER BY random()
                      LIMIT 1;'''
            cur.execute(cmd, (approx_sim, approx_sim))
        else:
            cur.execute(cmd)
        rec = cur.fetchone()
        e1, e2, s = rec
        return (e1, e2, s)

    def entity_data(self, eid):
        cur = self.conn.cursor()
        cmd = '''SELECT g.id, lf.id, COALESCE(g.name, lf.local_name), ld.value
                   FROM local_data ld
             INNER JOIN local_fields lf
                     ON ld.field_id = lf.id
              LEFT JOIN (SELECT ga.id, ga.name, am.local_id
                   FROM global_attributes ga, attribute_mappings am
                  WHERE ga.id = am.global_id) g
                     ON lf.id = g.local_id
                  WHERE ld.entity_id = %s;'''
        cur.execute(cmd, (int(eid),))
        data = {}
        for rec in cur.fetchall():
            gid, fid, name, value = rec
            value = '' if None else value
            data[name] = value
        # data.append({'global_id': gid, 'local_id': fid,
        # 'name': name, 'value': value})
        return data

    def save_entity_comparison(self, e1id, e2id, answer):
        cur = self.conn.cursor()
        cmd = '''UPDATE entity_similarities SET human_label = %s
                  WHERE entity_a = %s AND entity_b = %s;'''
        cur.execute(cmd, (answer, e1id, e2id))
        if answer == 'YES':
            cmd = '''INSERT INTO entity_matches SELECT %s, %s;'''
            cur.execute(cmd, (e1id, e2id))
        self.conn.commit()

    def sourcename(self, source_id):
            cur = self.conn.cursor()
            cmd = 'SELECT local_id FROM local_sources WHERE id = %s'
            cur.execute(cmd, (source_id,))
            self.conn.commit()
            return cur.fetchone()[0]

    def get_cluster_data(self, source_id):
        cur = self.conn.cursor()
        display_attr = 'TITLE'

        cmd = ''' select ec.entity_id, ec.cluster_id, display.value
                    from entity_clustering ec
                    left join
                    (
                        select ld.entity_id, ld.value
                        from local_fields lf,
                             local_data ld,
                             attribute_mappings ama,
                             global_attributes ga
                        where lf.id = ld.field_id
                          and ama.local_id = lf.id
                          and ama.global_id = ga.id
                          and ga.name = %s
                    ) display on display.entity_id = ec.entity_id
                  where ec.cluster_id in
                    (select cluster_id from entity_clustering
                     where entity_id in
                      (select id from local_entities where source_id = %s)); '''
        cur.execute(cmd, (display_attr, source_id))
        data = {"name": "%s" % self.sourcename(source_id)}
        lookup = {}
        self.conn.commit()
        for rec in cur.fetchall():
            entity_id, cluster_id, display_name = rec
            if display_name is None:
                display_name = entity_id
            res = lookup.setdefault(cluster_id, [])
            res.append({'id': entity_id, 'name': display_name, 'size': 1000 + random.randint(0, 1000)})
        children = []
        for id in lookup:
            children.append({'name': '%s' % id, 'children': lookup[id]})
        data['children'] = children
        return data

    def get_entity_data(self, entity_id):
        cur = self.conn.cursor()
        cmd = ''' SELECT lf.local_name, ld.value
                  FROM local_fields lf join local_data ld on lf.id = ld.field_id
                  WHERE ld.entity_id = %s
                  ORDER BY lf.local_name'''
        cur.execute(cmd, (entity_id,))
        self.conn.commit()
        return cur.fetchall()

    def get_simpairs(self, source_id):
        cur = self.conn.cursor()
        cmd = ''' select sp.*, ec1.cluster_id
                    from sim_pairs sp, entity_clustering ec1,
                         entity_clustering ec2, local_entities le
                    where ec1.entity_id = sp.entity1_id
                        and ec2.entity_id = sp.entity2_id
                        and ec2.cluster_id = ec1.cluster_id
                        and le.source_id = %s
                        and (sp.entity1_id = le.id or sp.entity2_id = le.id); '''
        cur.execute(cmd, (source_id,))
        self.conn.commit()
        pairs = []
        for item in cur.fetchall():
            e1, e2, prob, cluster = item
            pairs.append({'e1': e1, 'e2': e2, 'prob': prob, 'cluster': cluster})
        return pairs
