CREATE TABLE IF NOT EXISTS BrahmavidyaPaths
(
    Id SERIAL PRIMARY KEY,
    PathText TEXT NOT NULL,
    NormalizedText TEXT NOT NULL,
    CreatedAt TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UpdatedAt TIMESTAMP NULL,

    CONSTRAINT CK_BrahmavidyaPaths_PathText_NotEmpty
    CHECK (length(trim(PathText)) > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS
UX_BrahmavidyaPaths_NormalizedText
ON BrahmavidyaPaths(NormalizedText);

INSERT INTO BrahmavidyaPaths
(
    PathText,
    NormalizedText,
    CreatedAt
)
VALUES
('આ તો દેના બેંક છે, લેના બેંક નથી.','આ તો દેના બેંક છે, લેના બેંક નથી.',CURRENT_TIMESTAMP),
('રાજીપો એ જ મોક્ષ.','રાજીપો એ જ મોક્ષ.',CURRENT_TIMESTAMP),
('ક્ષમા આધ્યાત્મિક ખજાનો છે.','ક્ષમા આધ્યાત્મિક ખજાનો છે.',CURRENT_TIMESTAMP),
('સન્માન અને અપમાન બંને નાશવંત છે.','સન્માન અને અપમાન બંને નાશવંત છે.',CURRENT_TIMESTAMP),
('સંપનો અભાવ = અનંત જન્મ!','સંપનો અભાવ = અનંત જન્મ!',CURRENT_TIMESTAMP),
('સત્સંગ શિક્ષણ પરીક્ષા એ ગુરુ પૂજન છે.','સત્સંગ શિક્ષણ પરીક્ષા એ ગુરુ પૂજન છે.',CURRENT_TIMESTAMP)
ON CONFLICT (NormalizedText)
DO NOTHING;