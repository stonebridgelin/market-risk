-- 由 `market-risk db export-sql` 自动生成，不得手工修改。
-- 表结构：Alembic 版本 0008（migrations/versions/），数据库：sqlite。
-- 具体数据不在此导出，见 db/sql/README.md。

CREATE TABLE backtest_runs (
	run_id VARCHAR(64) NOT NULL,
	created_at_utc VARCHAR(40),
	git_commit VARCHAR(40),
	git_dirty BOOLEAN,
	market_manifest_sha256 VARCHAR(64),
	config_sha256 VARCHAR(64),
	start_date DATE NOT NULL,
	end_date DATE NOT NULL,
	versions VARCHAR(32) NOT NULL,
	days INTEGER NOT NULL,
	runtime_seconds NUMERIC(20, 8),
	holdout_unlocked_at VARCHAR(40),
	is_official BOOLEAN NOT NULL,
	run_dir VARCHAR(300) NOT NULL,
	PRIMARY KEY (run_id)
);

CREATE TABLE data_decisions (
	date DATE NOT NULL,
	symbol VARCHAR(40) NOT NULL,
	decision VARCHAR(16) NOT NULL,
	reason TEXT,
	decided_on DATE NOT NULL,
	corrected_value NUMERIC(20, 8),
	evidence_source TEXT,
	PRIMARY KEY (date, symbol)
);

CREATE TABLE market_holidays (
	market VARCHAR(8) NOT NULL,
	date DATE NOT NULL,
	kind VARCHAR(16) NOT NULL,
	note TEXT,
	PRIMARY KEY (market, date, kind)
);

CREATE TABLE materials (
	id INTEGER NOT NULL,
	subject VARCHAR(16) NOT NULL,
	base_date DATE NOT NULL,
	type VARCHAR(32) NOT NULL,
	file_path VARCHAR(300) NOT NULL,
	source TEXT,
	note TEXT,
	added_at VARCHAR(40),
	PRIMARY KEY (id)
);

CREATE TABLE outcomes (
	subject VARCHAR(16) NOT NULL,
	base_date DATE NOT NULL,
	source VARCHAR(16) NOT NULL,
	window_start DATE NOT NULL,
	window_end DATE NOT NULL,
	spx_drawdown_from_base NUMERIC(20, 8),
	qqq_drawdown_from_base NUMERIC(20, 8),
	is_event BOOLEAN NOT NULL,
	event_date DATE,
	entered_at VARCHAR(40),
	spx_peak_to_trough_drawdown NUMERIC(20, 8),
	qqq_peak_to_trough_drawdown NUMERIC(20, 8),
	near_event BOOLEAN,
	PRIMARY KEY (subject, base_date, source)
);

CREATE TABLE provenance_records (
	record_id VARCHAR(16) NOT NULL,
	indicator VARCHAR(40) NOT NULL,
	trade_date DATE NOT NULL,
	raw_value VARCHAR(40) NOT NULL,
	raw_unit VARCHAR(16) NOT NULL,
	raw_basis TEXT NOT NULL,
	raw_precision INTEGER NOT NULL,
	normalized_value NUMERIC(20, 8) NOT NULL,
	source TEXT NOT NULL,
	acquisition_method VARCHAR(16) NOT NULL,
	first_obtained_at_et VARCHAR(40),
	entered_at_utc VARCHAR(40) NOT NULL,
	entered_by VARCHAR(64) NOT NULL,
	is_late BOOLEAN,
	source_published_at VARCHAR(40),
	snapshot_path VARCHAR(300),
	snapshot_sha256 VARCHAR(64),
	data_version VARCHAR(64),
	code_version VARCHAR(40) NOT NULL,
	code_dirty BOOLEAN,
	revises_record_id VARCHAR(16),
	revision_kind VARCHAR(16),
	correction_original_value VARCHAR(40),
	correction_corrected_value VARCHAR(40),
	correction_evidence TEXT,
	historical_backfill BOOLEAN NOT NULL,
	PRIMARY KEY (record_id),
	FOREIGN KEY(revises_record_id) REFERENCES provenance_records (record_id)
);

CREATE TABLE reviews (
	id INTEGER NOT NULL,
	subject VARCHAR(16) NOT NULL,
	base_date DATE,
	reviewer VARCHAR(32) NOT NULL,
	category TEXT NOT NULL,
	content TEXT,
	impact TEXT,
	run_key VARCHAR(200),
	other_run_key VARCHAR(200),
	created_at VARCHAR(40),
	PRIMARY KEY (id)
);

CREATE TABLE runs (
	run_key VARCHAR(200) NOT NULL,
	run_id VARCHAR(64) NOT NULL,
	subject VARCHAR(16) NOT NULL,
	framework VARCHAR(40) NOT NULL,
	base_date DATE NOT NULL,
	mode VARCHAR(16),
	created_at_utc VARCHAR(40),
	created_at_local VARCHAR(40),
	git_commit VARCHAR(40),
	git_dirty BOOLEAN,
	data_source_type VARCHAR(16),
	status VARCHAR(16),
	run_dir VARCHAR(300),
	is_official BOOLEAN NOT NULL,
	reviewed BOOLEAN NOT NULL,
	official_set_by VARCHAR(32),
	PRIMARY KEY (run_key)
);

CREATE TABLE symbols (
	symbol VARCHAR(40) NOT NULL,
	tv_symbol VARCHAR(40) NOT NULL,
	name TEXT,
	category VARCHAR(32),
	usage VARCHAR(16) NOT NULL,
	unit VARCHAR(16),
	timezone VARCHAR(40),
	calendar VARCHAR(16),
	inception DATE,
	api_source VARCHAR(64),
	tolerance NUMERIC(20, 8),
	crosscheck_note TEXT,
	filename_aliases TEXT,
	known_values TEXT,
	PRIMARY KEY (symbol),
	UNIQUE (tv_symbol)
);

CREATE TABLE backtest_daily_scores (
	run_id VARCHAR(64) NOT NULL,
	base_date DATE NOT NULL,
	version VARCHAR(16) NOT NULL,
	price INTEGER,
	breadth INTEGER,
	vix INTEGER,
	rates INTEGER,
	credit INTEGER,
	price_possible VARCHAR(16),
	breadth_possible VARCHAR(16),
	vix_possible VARCHAR(16),
	rates_possible VARCHAR(16),
	credit_possible VARCHAR(16),
	total INTEGER,
	total_min INTEGER NOT NULL,
	total_max INTEGER NOT NULL,
	stage VARCHAR(16),
	pending_dimensions TEXT,
	clear_deterioration VARCHAR(8),
	alert VARCHAR(8),
	flags TEXT,
	PRIMARY KEY (run_id, base_date, version),
	FOREIGN KEY(run_id) REFERENCES backtest_runs (run_id)
);

CREATE TABLE backtest_outcomes (
	run_id VARCHAR(64) NOT NULL,
	base_date DATE NOT NULL,
	window_start DATE NOT NULL,
	window_end DATE NOT NULL,
	spx_drawdown_from_base NUMERIC(20, 8),
	qqq_drawdown_from_base NUMERIC(20, 8),
	spx_peak_to_trough_drawdown NUMERIC(20, 8),
	qqq_peak_to_trough_drawdown NUMERIC(20, 8),
	is_event BOOLEAN,
	event_date DATE,
	is_near_event BOOLEAN,
	period VARCHAR(16),
	crosses_period BOOLEAN NOT NULL,
	data_note TEXT,
	PRIMARY KEY (run_id, base_date),
	FOREIGN KEY(run_id) REFERENCES backtest_runs (run_id)
);

CREATE TABLE dimension_scores (
	id INTEGER NOT NULL,
	run_key VARCHAR(200) NOT NULL,
	version VARCHAR(16) NOT NULL,
	dimension VARCHAR(16) NOT NULL,
	score INTEGER,
	possible_scores VARCHAR(32),
	triggered_conditions TEXT,
	pending_reason TEXT,
	calculation TEXT,
	PRIMARY KEY (id),
	FOREIGN KEY(run_key) REFERENCES runs (run_key)
);

CREATE TABLE metrics (
	run_key VARCHAR(200) NOT NULL,
	"key" VARCHAR(64) NOT NULL,
	value NUMERIC(20, 8),
	text TEXT,
	PRIMARY KEY (run_key, "key"),
	FOREIGN KEY(run_key) REFERENCES runs (run_key)
);

CREATE TABLE near_threshold (
	id INTEGER NOT NULL,
	run_key VARCHAR(200) NOT NULL,
	item TEXT NOT NULL,
	value NUMERIC(20, 8),
	threshold NUMERIC(20, 8),
	gap NUMERIC(20, 8),
	unit VARCHAR(16),
	PRIMARY KEY (id),
	FOREIGN KEY(run_key) REFERENCES runs (run_key)
);

CREATE TABLE officials (
	subject VARCHAR(16) NOT NULL,
	framework VARCHAR(40) NOT NULL,
	base_date DATE NOT NULL,
	run_key VARCHAR(200) NOT NULL,
	set_by VARCHAR(32),
	reviewed BOOLEAN NOT NULL,
	set_at_utc VARCHAR(40),
	reviewed_at_utc VARCHAR(40),
	PRIMARY KEY (subject, framework, base_date),
	FOREIGN KEY(run_key) REFERENCES runs (run_key)
);

CREATE TABLE provenance_confirmations (
	record_id VARCHAR(16) NOT NULL,
	confirmed_by VARCHAR(64) NOT NULL,
	confirmed_at_utc VARCHAR(40) NOT NULL,
	self_confirmed BOOLEAN NOT NULL,
	PRIMARY KEY (record_id),
	FOREIGN KEY(record_id) REFERENCES provenance_records (record_id)
);

CREATE TABLE pullback_episodes (
	id INTEGER NOT NULL,
	run_id VARCHAR(64) NOT NULL,
	symbol VARCHAR(8) NOT NULL,
	level NUMERIC(20, 8) NOT NULL,
	high_date DATE NOT NULL,
	high_close NUMERIC(20, 8) NOT NULL,
	low_date DATE,
	low_close NUMERIC(20, 8),
	drawdown_pct NUMERIC(20, 8),
	trading_days INTEGER,
	grade VARCHAR(16),
	status VARCHAR(32) NOT NULL,
	confirm_date DATE,
	recovery_date DATE,
	recovery_note TEXT,
	period VARCHAR(16) NOT NULL,
	before_start BOOLEAN NOT NULL,
	crosses_boundary BOOLEAN NOT NULL,
	counted BOOLEAN NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(run_id) REFERENCES backtest_runs (run_id)
);

CREATE TABLE signal_input_links (
	signal_key VARCHAR(200) NOT NULL,
	record_id VARCHAR(16) NOT NULL,
	note TEXT,
	PRIMARY KEY (signal_key, record_id),
	FOREIGN KEY(record_id) REFERENCES provenance_records (record_id)
);

CREATE TABLE totals (
	run_key VARCHAR(200) NOT NULL,
	version VARCHAR(16) NOT NULL,
	total INTEGER,
	total_min INTEGER NOT NULL,
	total_max INTEGER NOT NULL,
	stage VARCHAR(16),
	clear_deterioration VARCHAR(8),
	alert VARCHAR(8),
	review_flags TEXT,
	notes TEXT,
	PRIMARY KEY (run_key, version),
	FOREIGN KEY(run_key) REFERENCES runs (run_key)
);

CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, PRIMARY KEY (version_num));
INSERT INTO alembic_version (version_num) VALUES ('0008');
