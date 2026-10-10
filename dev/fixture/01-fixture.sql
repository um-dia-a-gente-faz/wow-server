-- Minimal characters/auth/world rows for the dev stack (#389). Made-up data; only the
-- tables and columns tools/wowmap reads. Refresh: see docs/DEV-STACK.md.
CREATE DATABASE IF NOT EXISTS auth;
CREATE DATABASE IF NOT EXISTS characters;
CREATE DATABASE IF NOT EXISTS world;

CREATE TABLE auth.account (id INT UNSIGNED PRIMARY KEY, username VARCHAR(32) NOT NULL);
INSERT INTO auth.account VALUES (1, 'MOCKUSER');

CREATE TABLE characters.characters (
  guid INT UNSIGNED PRIMARY KEY, account INT UNSIGNED NOT NULL, name VARCHAR(12) NOT NULL,
  race TINYINT UNSIGNED NOT NULL, class TINYINT UNSIGNED NOT NULL, gender TINYINT UNSIGNED NOT NULL,
  level TINYINT UNSIGNED NOT NULL, money INT UNSIGNED NOT NULL DEFAULT 0,
  position_x FLOAT NOT NULL, position_y FLOAT NOT NULL, position_z FLOAT NOT NULL,
  orientation FLOAT NOT NULL DEFAULT 0, map SMALLINT UNSIGNED NOT NULL, instance_id INT UNSIGNED NOT NULL DEFAULT 0,
  zone SMALLINT UNSIGNED NOT NULL, totaltime INT UNSIGNED NOT NULL DEFAULT 0,
  logout_time INT UNSIGNED NOT NULL DEFAULT 0, online TINYINT UNSIGNED NOT NULL DEFAULT 0,
  health INT UNSIGNED NOT NULL DEFAULT 0,
  power1 INT UNSIGNED NOT NULL DEFAULT 0, power2 INT UNSIGNED NOT NULL DEFAULT 0,
  power3 INT UNSIGNED NOT NULL DEFAULT 0, power4 INT UNSIGNED NOT NULL DEFAULT 0,
  power5 INT UNSIGNED NOT NULL DEFAULT 0, power6 INT UNSIGNED NOT NULL DEFAULT 0,
  power7 INT UNSIGNED NOT NULL DEFAULT 0,
  exploredZones LONGTEXT
);
-- Luaprata, a level 5 Blood Elf Mage in Eversong Woods (same spot the wowmap transform uses).
INSERT INTO characters.characters (guid, account, name, race, class, gender, level, position_x, position_y,
  position_z, map, zone, online, health, power1)
VALUES (2, 1, 'Luaprata', 10, 8, 1, 5, 10345.2, -6349.35, 36.0, 530, 3430, 1, 100, 120);

CREATE TABLE characters.item_instance (guid INT UNSIGNED PRIMARY KEY, itemEntry INT UNSIGNED NOT NULL,
  count INT UNSIGNED NOT NULL DEFAULT 1);
CREATE TABLE characters.character_inventory (guid INT UNSIGNED NOT NULL, bag INT UNSIGNED NOT NULL,
  slot TINYINT UNSIGNED NOT NULL, item INT UNSIGNED NOT NULL, PRIMARY KEY (item));
CREATE TABLE characters.character_queststatus_rewarded (guid INT UNSIGNED NOT NULL,
  quest INT UNSIGNED NOT NULL, active TINYINT UNSIGNED NOT NULL DEFAULT 1, PRIMARY KEY (guid, quest));

CREATE TABLE world.item_template (entry INT UNSIGNED PRIMARY KEY, name VARCHAR(255) NOT NULL,
  SellPrice INT UNSIGNED NOT NULL DEFAULT 0);
CREATE TABLE world.quest_template (ID INT UNSIGNED PRIMARY KEY, LogTitle TEXT);
