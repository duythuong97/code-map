-- Dummy graph for UI filtering/navigation tests.
-- Apply only to a disposable SQLite database.
BEGIN IMMEDIATE;

DELETE FROM edges
WHERE from_qname LIKE 'Dummy:%' OR to_qname LIKE 'Dummy:%';
DELETE FROM nodes WHERE qualified_name LIKE 'Dummy:%';

INSERT INTO nodes(label, qualified_name, name, properties_json) VALUES
('Repository',   'Dummy:L01:Repository',   'L01 Repository',   '{"qualified_name":"Dummy:L01:Repository","name":"L01 Repository","dummy":true,"level":1}'),
('Application',  'Dummy:L02:Application',  'L02 Application',  '{"qualified_name":"Dummy:L02:Application","name":"L02 Application","dummy":true,"level":2}'),
('SourceFile',   'Dummy:L03:SourceFile',   'L03 SourceFile',   '{"qualified_name":"Dummy:L03:SourceFile","name":"L03 SourceFile","dummy":true,"level":3}'),
('PLSQLPackage', 'Dummy:L04:Package',      'L04 Package',      '{"qualified_name":"Dummy:L04:Package","name":"L04 Package","dummy":true,"level":4}'),
('Procedure',    'Dummy:L05:Procedure',    'L05 Procedure',    '{"qualified_name":"Dummy:L05:Procedure","name":"L05 Procedure","dummy":true,"level":5}'),
('SQLFunction',  'Dummy:L06:Function',     'L06 Function',     '{"qualified_name":"Dummy:L06:Function","name":"L06 Function","dummy":true,"level":6}'),
('Trigger',      'Dummy:L07:Trigger',      'L07 Trigger',      '{"qualified_name":"Dummy:L07:Trigger","name":"L07 Trigger","dummy":true,"level":7}'),
('Table',        'Dummy:L08:Table',        'L08 Table',        '{"qualified_name":"Dummy:L08:Table","name":"L08 Table","dummy":true,"level":8}'),
('Column',       'Dummy:L09:Column',       'L09 Column',       '{"qualified_name":"Dummy:L09:Column","name":"L09 Column","dummy":true,"level":9}'),
('Sequence',     'Dummy:L10:Sequence',     'L10 Sequence',     '{"qualified_name":"Dummy:L10:Sequence","name":"L10 Sequence","dummy":true,"level":10}'),
('Table',        'Dummy:Branch:AuditTable','Branch Audit Table','{"qualified_name":"Dummy:Branch:AuditTable","name":"Branch Audit Table","dummy":true,"branch":true}'),
('Procedure',    'Dummy:Branch:Worker',    'Branch Worker',    '{"qualified_name":"Dummy:Branch:Worker","name":"Branch Worker","dummy":true,"branch":true}');

INSERT INTO edges(from_qname, to_qname, rel_type, properties_json, source_file, line) VALUES
('Dummy:L01:Repository',    'Dummy:L02:Application',   'CONTAINS',       '{"dummy":true,"step":1}', 'dummy_graph_10_levels.sql', 1),
('Dummy:L02:Application',   'Dummy:L03:SourceFile',    'CONTAINS',       '{"dummy":true,"step":2}', 'dummy_graph_10_levels.sql', 2),
('Dummy:L03:SourceFile',    'Dummy:L04:Package',       'CONTAINS',       '{"dummy":true,"step":3}', 'dummy_graph_10_levels.sql', 3),
('Dummy:L04:Package',       'Dummy:L05:Procedure',     'CONTAINS',       '{"dummy":true,"step":4}', 'dummy_graph_10_levels.sql', 4),
('Dummy:L05:Procedure',     'Dummy:L06:Function',      'CALLS',          '{"dummy":true,"step":5}', 'dummy_graph_10_levels.sql', 5),
('Dummy:L06:Function',      'Dummy:L07:Trigger',       'TRIGGERS',       '{"dummy":true,"step":6}', 'dummy_graph_10_levels.sql', 6),
('Dummy:L07:Trigger',       'Dummy:L08:Table',         'WRITES_TO',      '{"dummy":true,"step":7}', 'dummy_graph_10_levels.sql', 7),
('Dummy:L08:Table',         'Dummy:L09:Column',        'BELONGS_TO',     '{"dummy":true,"step":8}', 'dummy_graph_10_levels.sql', 8),
('Dummy:L09:Column',        'Dummy:L10:Sequence',      'USES_SEQUENCE',  '{"dummy":true,"step":9}', 'dummy_graph_10_levels.sql', 9),
('Dummy:L05:Procedure',     'Dummy:L08:Table',         'READS_FROM',     '{"dummy":true}', 'dummy_graph_10_levels.sql', 20),
('Dummy:L05:Procedure',     'Dummy:Branch:AuditTable', 'INSERTS_INTO',   '{"dummy":true}', 'dummy_graph_10_levels.sql', 21),
('Dummy:Branch:Worker',     'Dummy:L08:Table',         'UPDATES',        '{"dummy":true}', 'dummy_graph_10_levels.sql', 22),
('Dummy:Branch:Worker',     'Dummy:Branch:AuditTable', 'DELETES_FROM',   '{"dummy":true}', 'dummy_graph_10_levels.sql', 23),
('Dummy:L06:Function',      'Dummy:Branch:AuditTable', 'MERGES_INTO',    '{"dummy":true}', 'dummy_graph_10_levels.sql', 24),
('Dummy:L06:Function',      'Dummy:Branch:Worker',     'CALLS',          '{"dummy":true,"branch":true}', 'dummy_graph_10_levels.sql', 25),
('Dummy:Branch:Worker',     'Dummy:L05:Procedure',     'CALLS',          '{"dummy":true,"cycle":true}', 'dummy_graph_10_levels.sql', 26);

COMMIT;
