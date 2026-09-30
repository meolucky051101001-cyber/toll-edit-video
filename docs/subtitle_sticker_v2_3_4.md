# Compact subtitle sticker — pipeline 2.3.4

The translated subtitle background keeps the compact sticker proportions from
the previous renderer while using the OCR-selected Chinese subtitle band for
placement.

- Maximum visible width is about 78% of the video frame. The supplied reference
  screenshot measures about 77% when the app's black action rail is excluded.
- A two-line sticker is about 9.4% of frame height.
- OCR source width can expand the cover only up to this compact limit.
- The 12px same-colour ASS outline covers the source glyph edges without adding
  large internal whitespace.
- Pipeline implementation version 2.3.4 invalidates cached subtitle/render
  artifacts made with the oversized 2.3.3 card.
