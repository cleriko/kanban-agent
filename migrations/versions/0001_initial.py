"""Initial schema.

Revision ID: 0001
Revises:
Create Date: 2026-09-18

Frozen snapshot of the schema as deployed. Earlier this migration called
``Base.metadata.create_all`` at run time, which meant a future model change would
silently give fresh databases a different schema from upgraded ones. The DDL below
is generated once and must never follow the models again: every later change is a
new hand-written revision.
"""
from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_POSTGRES = [
    'CREATE TABLE agent_actions (\n\tid VARCHAR(36) NOT NULL, \n\tconversation_id VARCHAR(36), \n\ttool VARCHAR(64) NOT NULL, \n\targuments JSON NOT NULL, \n\tsummary VARCHAR(500) NOT NULL, \n\tdestructive BOOLEAN NOT NULL, \n\texecuted BOOLEAN NOT NULL, \n\texecuted_at TIMESTAMP WITH TIME ZONE, \n\tresult JSON, \n\terror TEXT, \n\tundo_state JSON, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id)\n);',
    'CREATE INDEX ix_agent_actions_conversation_id ON agent_actions (conversation_id);',
    'CREATE INDEX ix_agent_actions_created_at ON agent_actions (created_at);',
    'CREATE INDEX ix_agent_actions_executed ON agent_actions (executed);',
    'CREATE INDEX ix_agent_actions_tool ON agent_actions (tool);',
    'CREATE TABLE meetings (\n\tid VARCHAR(36) NOT NULL, \n\tuser_id VARCHAR(36), \n\ttitle VARCHAR(500) NOT NULL, \n\tstarted_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tduration FLOAT NOT NULL, \n\tstatus VARCHAR(12) NOT NULL, \n\taudio_key VARCHAR(500), \n\taudio_bytes INTEGER NOT NULL, \n\taudio_content_type VARCHAR(100), \n\tsummary TEXT NOT NULL, \n\tkey_points JSON NOT NULL, \n\tdecisions JSON NOT NULL, \n\tquestions JSON NOT NULL, \n\ttopics JSON NOT NULL, \n\tparticipants JSON NOT NULL, \n\terror TEXT, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tprocessed_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id)\n);',
    'CREATE INDEX ix_meetings_started_at ON meetings (started_at);',
    'CREATE INDEX ix_meetings_status ON meetings (status);',
    'CREATE INDEX ix_meetings_user_id ON meetings (user_id);',
    'CREATE TABLE users (\n\tid VARCHAR(36) NOT NULL, \n\temail VARCHAR(320) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (email)\n);',
    'CREATE TABLE meeting_action_items (\n\tid VARCHAR(36) NOT NULL, \n\tmeeting_id VARCHAR(36) NOT NULL, \n\ttitle VARCHAR(500) NOT NULL, \n\tassignee VARCHAR(200), \n\tdue_date TIMESTAMP WITH TIME ZONE, \n\tconfidence FLOAT NOT NULL, \n\tstate VARCHAR(9) NOT NULL, \n\ttask_id VARCHAR(36), \n\tposition FLOAT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(meeting_id) REFERENCES meetings (id) ON DELETE CASCADE\n);',
    'CREATE INDEX ix_meeting_action_items_meeting_id ON meeting_action_items (meeting_id);',
    'CREATE TABLE meeting_decisions (\n\tid VARCHAR(36) NOT NULL, \n\tmeeting_id VARCHAR(36) NOT NULL, \n\ttext TEXT NOT NULL, \n\tposition FLOAT NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(meeting_id) REFERENCES meetings (id) ON DELETE CASCADE\n);',
    'CREATE INDEX ix_meeting_decisions_meeting_id ON meeting_decisions (meeting_id);',
    'CREATE TABLE meeting_transcripts (\n\tid VARCHAR(36) NOT NULL, \n\tmeeting_id VARCHAR(36) NOT NULL, \n\tlanguage VARCHAR(16), \n\tprovider VARCHAR(64), \n\tmodel VARCHAR(128), \n\tduration FLOAT NOT NULL, \n\ttext TEXT NOT NULL, \n\tsegments JSON NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(meeting_id) REFERENCES meetings (id) ON DELETE CASCADE\n);',
    'CREATE UNIQUE INDEX ix_meeting_transcripts_meeting_id ON meeting_transcripts (meeting_id);',
    'CREATE TABLE processing_jobs (\n\tid VARCHAR(36) NOT NULL, \n\tkind VARCHAR(64) NOT NULL, \n\tmeeting_id VARCHAR(36), \n\tstatus VARCHAR(9) NOT NULL, \n\tstage VARCHAR(64), \n\tprogress FLOAT NOT NULL, \n\terror TEXT, \n\tattempts INTEGER NOT NULL, \n\tlocked_at TIMESTAMP WITH TIME ZONE, \n\tlocked_by VARCHAR(100), \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tfinished_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(meeting_id) REFERENCES meetings (id) ON DELETE CASCADE\n);',
    'CREATE INDEX ix_jobs_claim ON processing_jobs (status, locked_at, created_at);',
    'CREATE INDEX ix_processing_jobs_created_at ON processing_jobs (created_at);',
    'CREATE INDEX ix_processing_jobs_kind ON processing_jobs (kind);',
    'CREATE INDEX ix_processing_jobs_meeting_id ON processing_jobs (meeting_id);',
    'CREATE INDEX ix_processing_jobs_status ON processing_jobs (status);',
    'CREATE TABLE tasks (\n\tid VARCHAR(36) NOT NULL, \n\tuser_id VARCHAR(36), \n\ttitle VARCHAR(500) NOT NULL, \n\tnotes TEXT NOT NULL, \n\tstatus VARCHAR(11) NOT NULL, \n\tpriority VARCHAR(6) NOT NULL, \n\tdue_date TIMESTAMP WITH TIME ZONE, \n\ttags JSON NOT NULL, \n\tsubtasks JSON NOT NULL, \n\tposition FLOAT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcompleted_at TIMESTAMP WITH TIME ZONE, \n\tarchived_at TIMESTAMP WITH TIME ZONE, \n\tsource VARCHAR(7) NOT NULL, \n\tmeeting_id VARCHAR(36), \n\taction_item_id VARCHAR(36), \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(meeting_id) REFERENCES meetings (id) ON DELETE SET NULL\n);',
    'CREATE INDEX ix_tasks_action_item_id ON tasks (action_item_id);',
    'CREATE INDEX ix_tasks_due_date ON tasks (due_date);',
    'CREATE INDEX ix_tasks_meeting_id ON tasks (meeting_id);',
    'CREATE INDEX ix_tasks_status ON tasks (status);',
    'CREATE INDEX ix_tasks_status_position ON tasks (status, position);',
    'CREATE INDEX ix_tasks_user_id ON tasks (user_id);',
]

_SQLITE = [
    'CREATE TABLE agent_actions (\n\tid VARCHAR(36) NOT NULL, \n\tconversation_id VARCHAR(36), \n\ttool VARCHAR(64) NOT NULL, \n\targuments JSON NOT NULL, \n\tsummary VARCHAR(500) NOT NULL, \n\tdestructive BOOLEAN NOT NULL, \n\texecuted BOOLEAN NOT NULL, \n\texecuted_at DATETIME, \n\tresult JSON, \n\terror TEXT, \n\tundo_state JSON, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id)\n);',
    'CREATE INDEX ix_agent_actions_conversation_id ON agent_actions (conversation_id);',
    'CREATE INDEX ix_agent_actions_created_at ON agent_actions (created_at);',
    'CREATE INDEX ix_agent_actions_executed ON agent_actions (executed);',
    'CREATE INDEX ix_agent_actions_tool ON agent_actions (tool);',
    'CREATE TABLE meetings (\n\tid VARCHAR(36) NOT NULL, \n\tuser_id VARCHAR(36), \n\ttitle VARCHAR(500) NOT NULL, \n\tstarted_at DATETIME NOT NULL, \n\tduration FLOAT NOT NULL, \n\tstatus VARCHAR(12) NOT NULL, \n\taudio_key VARCHAR(500), \n\taudio_bytes INTEGER NOT NULL, \n\taudio_content_type VARCHAR(100), \n\tsummary TEXT NOT NULL, \n\tkey_points JSON NOT NULL, \n\tdecisions JSON NOT NULL, \n\tquestions JSON NOT NULL, \n\ttopics JSON NOT NULL, \n\tparticipants JSON NOT NULL, \n\terror TEXT, \n\tcreated_at DATETIME NOT NULL, \n\tupdated_at DATETIME NOT NULL, \n\tprocessed_at DATETIME, \n\tPRIMARY KEY (id)\n);',
    'CREATE INDEX ix_meetings_started_at ON meetings (started_at);',
    'CREATE INDEX ix_meetings_status ON meetings (status);',
    'CREATE INDEX ix_meetings_user_id ON meetings (user_id);',
    'CREATE TABLE users (\n\tid VARCHAR(36) NOT NULL, \n\temail VARCHAR(320) NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (email)\n);',
    'CREATE TABLE meeting_action_items (\n\tid VARCHAR(36) NOT NULL, \n\tmeeting_id VARCHAR(36) NOT NULL, \n\ttitle VARCHAR(500) NOT NULL, \n\tassignee VARCHAR(200), \n\tdue_date DATETIME, \n\tconfidence FLOAT NOT NULL, \n\tstate VARCHAR(9) NOT NULL, \n\ttask_id VARCHAR(36), \n\tposition FLOAT NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(meeting_id) REFERENCES meetings (id) ON DELETE CASCADE\n);',
    'CREATE INDEX ix_meeting_action_items_meeting_id ON meeting_action_items (meeting_id);',
    'CREATE TABLE meeting_decisions (\n\tid VARCHAR(36) NOT NULL, \n\tmeeting_id VARCHAR(36) NOT NULL, \n\ttext TEXT NOT NULL, \n\tposition FLOAT NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(meeting_id) REFERENCES meetings (id) ON DELETE CASCADE\n);',
    'CREATE INDEX ix_meeting_decisions_meeting_id ON meeting_decisions (meeting_id);',
    'CREATE TABLE meeting_transcripts (\n\tid VARCHAR(36) NOT NULL, \n\tmeeting_id VARCHAR(36) NOT NULL, \n\tlanguage VARCHAR(16), \n\tprovider VARCHAR(64), \n\tmodel VARCHAR(128), \n\tduration FLOAT NOT NULL, \n\ttext TEXT NOT NULL, \n\tsegments JSON NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(meeting_id) REFERENCES meetings (id) ON DELETE CASCADE\n);',
    'CREATE UNIQUE INDEX ix_meeting_transcripts_meeting_id ON meeting_transcripts (meeting_id);',
    'CREATE TABLE processing_jobs (\n\tid VARCHAR(36) NOT NULL, \n\tkind VARCHAR(64) NOT NULL, \n\tmeeting_id VARCHAR(36), \n\tstatus VARCHAR(9) NOT NULL, \n\tstage VARCHAR(64), \n\tprogress FLOAT NOT NULL, \n\terror TEXT, \n\tattempts INTEGER NOT NULL, \n\tlocked_at DATETIME, \n\tlocked_by VARCHAR(100), \n\tcreated_at DATETIME NOT NULL, \n\tupdated_at DATETIME NOT NULL, \n\tfinished_at DATETIME, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(meeting_id) REFERENCES meetings (id) ON DELETE CASCADE\n);',
    'CREATE INDEX ix_jobs_claim ON processing_jobs (status, locked_at, created_at);',
    'CREATE INDEX ix_processing_jobs_created_at ON processing_jobs (created_at);',
    'CREATE INDEX ix_processing_jobs_kind ON processing_jobs (kind);',
    'CREATE INDEX ix_processing_jobs_meeting_id ON processing_jobs (meeting_id);',
    'CREATE INDEX ix_processing_jobs_status ON processing_jobs (status);',
    'CREATE TABLE tasks (\n\tid VARCHAR(36) NOT NULL, \n\tuser_id VARCHAR(36), \n\ttitle VARCHAR(500) NOT NULL, \n\tnotes TEXT NOT NULL, \n\tstatus VARCHAR(11) NOT NULL, \n\tpriority VARCHAR(6) NOT NULL, \n\tdue_date DATETIME, \n\ttags JSON NOT NULL, \n\tsubtasks JSON NOT NULL, \n\tposition FLOAT NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tupdated_at DATETIME NOT NULL, \n\tcompleted_at DATETIME, \n\tarchived_at DATETIME, \n\tsource VARCHAR(7) NOT NULL, \n\tmeeting_id VARCHAR(36), \n\taction_item_id VARCHAR(36), \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(meeting_id) REFERENCES meetings (id) ON DELETE SET NULL\n);',
    'CREATE INDEX ix_tasks_action_item_id ON tasks (action_item_id);',
    'CREATE INDEX ix_tasks_due_date ON tasks (due_date);',
    'CREATE INDEX ix_tasks_meeting_id ON tasks (meeting_id);',
    'CREATE INDEX ix_tasks_status ON tasks (status);',
    'CREATE INDEX ix_tasks_status_position ON tasks (status, position);',
    'CREATE INDEX ix_tasks_user_id ON tasks (user_id);',
]



def upgrade() -> None:
    # Same logical schema, dialect-correct types (TIMESTAMPTZ vs DATETIME, etc.).
    statements = _POSTGRES if op.get_bind().dialect.name == "postgresql" else _SQLITE
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tasks;")
    op.execute("DROP TABLE IF EXISTS processing_jobs;")
    op.execute("DROP TABLE IF EXISTS meeting_transcripts;")
    op.execute("DROP TABLE IF EXISTS meeting_decisions;")
    op.execute("DROP TABLE IF EXISTS meeting_action_items;")
    op.execute("DROP TABLE IF EXISTS users;")
    op.execute("DROP TABLE IF EXISTS meetings;")
    op.execute("DROP TABLE IF EXISTS agent_actions;")
