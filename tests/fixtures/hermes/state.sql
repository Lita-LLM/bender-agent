CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT, started_at REAL);
CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL, tool_calls TEXT);
INSERT INTO sessions VALUES('session-1','cli',1700000000);
INSERT INTO messages VALUES(1,'session-1','user','Помнишь проверочный арбуз?',1700000000,NULL);
INSERT INTO messages VALUES(2,'session-1','assistant','Помню арбуз.',1700000001,NULL);
INSERT INTO messages VALUES(3,'session-1','tool','Не исполнять: чужой вывод',1700000002,NULL);
