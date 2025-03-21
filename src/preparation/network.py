import json
import sys
import os
import subprocess
import psycopg2
import time
from src.config.config import Config
from src.db.db import Database
from src.utils.utils import (
    print_info,
    print_hashtags,
    create_table_dump,
    create_table_schema,
)
from multiprocessing.pool import Pool
from src.collection.osm_collection_base import OSMBaseCollection
from src.preparation.network_islands import NetworkIslands
from src.utils.utils import create_table_schema, create_pgpass, restore_table_dump
from src.core.config import settings
from functools import partial

class NetworkPreparation:
    """Class to prepare the routing network. It processs the network in chunks and prepares the different attributes (e.g., slopes)."""

    def __init__(self, db: Database, db_rd: Database, config: Config):
        self.db = db
        self.db_rd = db_rd
        self.db_config = db.db_config
        self.dbname = self.db_config.path[1:]
        self.user = self.db_config.user
        self.host = self.db_config.host
        self.port = self.db_config.port
        self.password = self.db_config.password
        self.config_ways_preparation = config.preparation
        self.available_cpus = os.cpu_count()

    def create_processing_units(self):
        sql_create_table = """
            DROP TABLE IF EXISTS processing_units; 
            CREATE TABLE processing_units (id serial, geom GEOMETRY(MULTIPOLYGON, 4326));
        """
        sql_fill_table = """
            INSERT INTO processing_units(geom)
            WITH boundaries AS 
            (
                SELECT ST_MakePolygon(geom) As geom, (ST_AREA(ST_MakePolygon(geom)::geography) / 100000000)::integer AS area_square_km 
                FROM 
                (
                SELECT ST_ExteriorRing((ST_Dump(st_union(geom))).geom) As geom
                FROM network_osm_boundary 
                ) s
            )
            SELECT ST_MULTI(create_equal_area_split_polygon(b.geom, 1))
            FROM boundaries b;
        """

        sql_create_index = """
            ALTER TABLE processing_units ADD PRIMARY KEY(id);
            CREATE INDEX ON processing_units USING GIST(geom);
        """

        sql_add_ways_status_column = """
            ALTER TABLE ways DROP COLUMN IF EXISTS preparation_status;
            ALTER TABLE ways ADD COLUMN preparation_status char(1);
        """
        self.db.perform(sql_create_table)
        self.db.perform(sql_fill_table)
        self.db.perform(sql_create_index)
        self.db.perform(sql_add_ways_status_column)

    def create_edge_indizes(self):
        sql_create_index = """
        ALTER TABLE basic.edge ADD PRIMARY KEY(id);
        CREATE INDEX idx_edge_geom ON basic.edge USING gist (geom);
        CREATE INDEX ix_basic_edge_bicycle ON basic.edge USING btree (bicycle);
        CREATE INDEX ix_basic_edge_edge_id ON basic.edge USING btree (edge_id);
        CREATE INDEX ix_basic_edge_foot ON basic.edge USING btree (foot);
        CREATE INDEX ix_basic_edge_source ON basic.edge USING btree (source);
        CREATE INDEX ix_basic_edge_target ON basic.edge USING btree (target);
        CREATE INDEX ix_basic_node_geom ON basic.node USING gist (geom);
        """
        self.db.perform(sql_create_index)

    def update_network_ids(self):
        """Update the network ids with preset values from existing network to be unique."""

        # Check if the network tables exist
        if not self.db.table_exists("node", "basic") or not self.db.table_exists("edge", "basic"):
            print_info("Network tables do not exist. Network ids will not be updated.")
            return

        # Get the maximum node and edge ids from the existing network tables
        previous_node_id = self.db_rd.select("SELECT MAX(id) FROM basic.node;")
        previous_node_id = previous_node_id[0][0]
        previous_edge_id = self.db_rd.select("SELECT MAX(id) FROM basic.edge;")
        previous_edge_id = previous_edge_id[0][0]

        # Update the node ids to match the ids from existing network tables
        sql_create_node_columns = f"""
            ALTER TABLE basic.node 
            ADD COLUMN new_id integer, ADD COLUMN cnt serial;
        """
        self.db.perform(sql_create_node_columns)

        sql_update_node_id = f"""
            UPDATE basic.node
            SET new_id = cnt + {previous_node_id};
        """
        self.db.perform(sql_update_node_id)

        sql_update_edge_nodes = f"""
            UPDATE basic.edge e 
            SET source = n.new_id
            FROM basic.node n  
            WHERE n.id = e.source;

            UPDATE basic.edge e 
            SET target = n.new_id
            FROM basic.node n  
            WHERE n.id = e.target;

            ALTER TABLE basic.node
            DROP COLUMN id;
            ALTER TABLE basic.node 
            RENAME COLUMN new_id TO id;
            ALTER TABLE basic.node
            DROP COLUMN cnt;
        """
        self.db.perform(sql_update_edge_nodes)

        # Update the edge ids to match the ids from existing network tables
        sql_create_edge_columns = """ALTER TABLE basic.edge
        ADD COLUMN new_id integer, ADD COLUMN cnt serial;
        """
        self.db.perform(sql_create_edge_columns)

        sql_update_edge_ids = f"""
        UPDATE basic.edge
        SET id = cnt + {previous_edge_id};
        ALTER TABLE basic.edge
        DROP COLUMN cnt; 
        """
        self.db.perform(sql_update_edge_ids)
        
        # Drop the new_id column that is not needed anymore   
        self.db.perform("ALTER TABLE basic.edge DROP COLUMN new_id;")
        

    def create_street_crossings(self):
        sql_street_crossings = """
            --Create table that stores all street crossings
            DROP TABLE IF EXISTS extra.street_crossings;
            CREATE TABLE extra.street_crossings AS 
            SELECT osm_id, NULL as key, highway,
            CASE WHEN (tags -> 'crossing_ref') IS NOT NULL THEN (tags -> 'crossing_ref') ELSE (tags -> 'crossing') END AS crossing, 
            (tags -> 'traffic_signals') AS traffic_signals, (tags -> 'kerb') AS kerb, 
            (tags -> 'segregated') AS segregated, (tags -> 'supervised') AS supervised, 
            (tags -> 'tactile_paving') AS tactile_paving, (tags -> 'wheelchair') AS wheelchair, way AS geom, 'osm' as source
            FROM planet_osm_point p
            WHERE (tags -> 'crossing') IS NOT NULL 
            OR highway IN('crossing','traffic_signals')
            OR (tags -> 'traffic_signals') = 'pedestrian_crossing';

            ALTER TABLE extra.street_crossings ADD COLUMN id serial;
            ALTER TABLE extra.street_crossings ADD PRIMARY key(id);

            UPDATE extra.street_crossings 
            SET crossing = 'traffic_signals'
            WHERE traffic_signals = 'crossing' 
            OR traffic_signals = 'pedestrian_crossing';

            UPDATE extra.street_crossings 
            SET crossing = highway 
            WHERE crossing IS NULL AND highway IS NOT NULL; 

            CREATE INDEX ON extra.street_crossings USING GIST(geom);
        """
        self.db.perform(query=sql_street_crossings)

# These functions are not in the class as there where difficulaties when running it in parallel
def prepare_ways_one_core(processing_unit_id: list[int], region: str):

    # Get seperate connection to the database
    connection_string = f"dbname={settings.POSTGRES_DB} user={settings.POSTGRES_USER} password={settings.POSTGRES_PASSWORD} host={settings.POSTGRES_HOST} port={settings.POSTGRES_PORT}"
    conn = psycopg2.connect(connection_string)
    cur = conn.cursor()
    config_ways_preparation = Config("network", region).preparation
    impedance_surface_object = json.dumps(config_ways_preparation["cycling_surface"])

    sql_select_ways_ids = f"""
        SELECT ARRAY_AGG(w.gid) AS ways_ids  
        FROM ways w, processing_units p 
        WHERE ST_Intersects(ST_CENTROID(w.the_geom), p.geom) 
        AND w.the_geom && p.geom
        AND p.id = {processing_unit_id}
        AND w.preparation_status IS NULL;
    """
    ways_ids = cur.execute(sql_select_ways_ids)
    ways_ids = cur.fetchall()
    ways_ids = ways_ids[0][0]

    # Check if there are way_ids
    if ways_ids is not None:
        cnt = 0
        for way_id in ways_ids:
            cnt += 1
            sql_perform_preparation = f"""
            SELECT classify_way(
                {way_id}, 
                ARRAY{config_ways_preparation["excluded_class_id_walking"]}, 
                ARRAY{config_ways_preparation["excluded_class_id_cycling"]},
                ARRAY{config_ways_preparation["categories_no_foot"]},
                ARRAY{config_ways_preparation["categories_no_bicycle"]},
                '{impedance_surface_object}'::jsonb
            );"""

            try:
                cur.execute(sql_perform_preparation)
                # Log success
                cur.execute(
                    f"""
                    UPDATE ways SET
                    preparation_status = 'p'
                    WHERE gid = {way_id};
                    """
                )
                conn.commit()

            except:
                conn.rollback()
                print(f"Error in processing way {way_id}")
                # Log error
                cur.execute(
                    f"""
                UPDATE ways SET 
                preparation_status = 'e'
                WHERE gid = {way_id};
                """
                )
                conn.commit()
                continue

    conn.close()
    print_hashtags()
    print("Finished processing unit: ", processing_unit_id)
    print_hashtags()


def prepare_ways(db, region: str):
    sql_delete_network = """
        TRUNCATE TABLE basic.edge;
    """
    db.perform(sql_delete_network)

    sql_read_processing_units = f"""
        SELECT id
        FROM processing_units;		
    """
    processing_units = db.select(sql_read_processing_units)
    processing_units = [u[0] for u in processing_units]
    print_hashtags()
    print_info(f"Start processing ways.")
    print_hashtags()
    start_time = time.time()

    # Execute in parallel 100 processing units at a time
    for i in range(0, len(processing_units), 100):
        processing_unit_ids = processing_units[i : i + 100]
        pool = Pool(processes=os.cpu_count())
        func = partial(prepare_ways_one_core, region=region)
        pool.map(func, (processing_unit_ids))
        pool.close()
        pool.join()
    print_hashtags()
    print(f"Calculation took {time.time() - start_time} seconds ---")
    print_hashtags()


def prepare_network(region: str):
    db = Database(settings.LOCAL_DATABASE_URI)
    db_rd = Database(settings.RAW_DATABASE_URI)
    osm_collection = OSMBaseCollection(
        db_config=db.db_config, dataset_type="network", region=region
    )

    # Import needed data into the database
    osm_collection.import_dem()

    # Prepare network
    config = Config("network", region)
    config.download_db_schema()
    preparation = NetworkPreparation(db, db_rd, config)
    create_table_schema(db, 'basic.edge')
    create_table_schema(db, 'basic.node')
    db.perform(query="CREATE INDEX ix_basic_node_id ON basic.node (id);")
    preparation.create_processing_units()
    prepare_ways(db, region)
    preparation.create_edge_indizes()
    NetworkIslands(db.db_config, config).find_network_islands()
    preparation.create_street_crossings()
    preparation.update_network_ids()
    db.conn.close()

def export_network(region: str):
    db = Database(settings.LOCAL_DATABASE_URI)
    db_rd = Database(settings.RAW_DATABASE_URI)
    create_table_dump(db.db_config, "basic", "node", data_only=True)
    create_table_dump(db.db_config, "basic", "edge", data_only=True)
    restore_table_dump(db_rd.db_config, "basic", "node", data_only=True)
    restore_table_dump(db_rd.db_config, "basic", "edge", data_only=True)
