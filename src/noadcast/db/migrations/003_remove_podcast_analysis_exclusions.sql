-- Retire per-podcast analysis exclusions. Preserve the old column and wire
-- field for clients that still know them, but re-enable every existing feed.
-- Assign a distinct sync seq to each changed podcast so /sync paging remains
-- lossless even when a migration changes more rows than one page can hold.
CREATE TEMP TABLE podcast_analysis_reset AS
  SELECT id, row_number() OVER (ORDER BY id) AS seq_offset
  FROM podcasts WHERE ad_analysis_enabled = 0;

UPDATE podcasts SET
  ad_analysis_enabled = 1,
  updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
  updated_seq = (SELECT seq FROM sync_state WHERE id = 1) +
                (SELECT seq_offset FROM podcast_analysis_reset WHERE id = podcasts.id)
WHERE id IN (SELECT id FROM podcast_analysis_reset);

UPDATE sync_state SET seq = seq + (SELECT count(*) FROM podcast_analysis_reset) WHERE id = 1;
DROP TABLE podcast_analysis_reset;
