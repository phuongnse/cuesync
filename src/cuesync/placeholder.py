"""The subtitle shown while a sync job is still running."""

from __future__ import annotations

from cuesync.subtitles import Cue

FALLBACK = "eng"
# Long enough to show wherever the user is in the video.
DURATION_MS = 10 * 3600 * 1000

# Keyed by the three-letter language code. Any other language gets the English text.
MESSAGES = {
    "eng": "CueSync is syncing this subtitle to your video. "
    "Reopen the subtitle menu in a minute or two.",
    "vie": "CueSync đang đồng bộ phụ đề này với video của bạn. "
    "Hãy mở lại menu phụ đề sau một hai phút.",
    "fre": "CueSync synchronise ce sous-titre avec votre vidéo. "
    "Rouvrez le menu des sous-titres dans une minute ou deux.",
    "spa": "CueSync está sincronizando este subtítulo con tu vídeo. "
    "Vuelve a abrir el menú de subtítulos en uno o dos minutos.",
    "ger": "CueSync synchronisiert diesen Untertitel mit deinem Video. "
    "Öffne das Untertitelmenü in ein bis zwei Minuten erneut.",
    "por": "O CueSync está sincronizando esta legenda com o seu vídeo. "
    "Abra o menu de legendas novamente em um ou dois minutos.",
    "ita": "CueSync sta sincronizzando questo sottotitolo con il tuo video. "
    "Riapri il menu dei sottotitoli tra un paio di minuti.",
}


def placeholder_cues(language: str) -> tuple[Cue, ...]:
    """One cue that says the subtitle is being synced, in `language` or else in English."""
    return (Cue(0, DURATION_MS, MESSAGES.get(language, MESSAGES[FALLBACK])),)
