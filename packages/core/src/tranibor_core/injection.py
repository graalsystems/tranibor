# SPDX-License-Identifier: Apache-2.0
"""Détection basique d'injection de prompt dans ce que l'agent va lire.

Un ticket, un commentaire, un document ou un souvenir de la mémoire est un texte que
personne de la plateforme n'a écrit : il peut porter des instructions déguisées en
contenu. La détection ici est **lexicale et honnête** — des motifs connus, pas une
compréhension. Elle ne remplace ni le bac à sable ni la vérification du diff (ADR 0010) ;
elle fait ce qu'OpenHands fait : voir venir les cas grossiers, et le dire.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Spanish, Mexican usage included (S40-28b). Written from how injections are phrased, not from the
#: corpus; measured on `tests/fixtures/injection/corpus.yaml` and the held-out
#: `heldout-es-fr.yaml` (docs/security.md). A pattern may be set to `block`: each one needs the
#: instructions, the model or the secret to be named, so "ignora el ticket anterior", "anula la
#: configuración por defecto" or "no le digas al usuario la contraseña" stay unflagged.
_SPANISH: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        # A tú or usted imperative (not the infinitive or "ignoren" a team writes), the instructions,
        # then what makes them the model's: earlier, initial, the system's, the safety ones.
        re.compile(
            r"\b(ignora|ignore|olvida|olvide|omite|omita|descarta|descarte|desatiende|desatienda"
            r"|haz caso omiso|haga caso omiso|no hagas caso|no haga caso|deja de seguir|deje de seguir)\b"
            r"[^.\n]{0,40}\b(instrucciones|indicaciones|reglas|[oó]rdenes|directrices|directivas|pautas)\b"
            r"[^.\n]{0,30}\b(anteriores|previas|de antes|iniciales|originales|del sistema|de sistema"
            r"|de seguridad)\b",
            re.I,
        ),
        "ignore the previous instructions",
    ),
    (
        re.compile(
            r"\b(ignora|ignore|olvida|olvide|omite|omita|descarta|descarte|desatiende|desatienda)\s+"
            r"(todas\s+)?(tus|sus)\s+(instrucciones|indicaciones|reglas|[oó]rdenes|directrices|pautas)\b"
            r"|\b(olvida|olvide|ignora|ignore)\s+todo\s+lo\s+(anterior|que\s+(te|le)\s+(dijeron|indicaron"
            r"|ordenaron|pidieron|programaron|configuraron|ense[nñ]aron))\b",
            re.I,
        ),
        "ignore the previous instructions",
    ),
    (
        # "A partir de ahora eres...": never a bare "ahora eres" (a service desk writes "listo, ahora
        # eres miembro del grupo"), never "es" ("a partir de ahora es obligatorio usar la VPN").
        re.compile(
            r"\b(a partir de ahora|desde ahora|de ahora en adelante)\s*,?\s*(t[uú]\s+)?"
            r"(eres|ser[aá]s|actuar[aá]s|act[uú]as|te comportar[aá]s|vas a ser|vas a actuar)\b"
            r"|\bya no eres\b",
            re.I,
        ),
        "role reassignment",
    ),
    (
        # "Actúa como" is also "el switch actúa como gateway": only at the start of a sentence.
        re.compile(
            r"(?:^|[.:;!?¡\n]\s*)(act[uú]a|act[uú]e|comp[oó]rtate|comp[oó]rtese)\s+como\b"
            r"|\b(haz|haga)\s+de\s+cuenta\s+que\s+(eres|es)\b"
            r"|\b(haz|haga)\s+como\s+si\s+(fueras|fuera)\b"
            r"|\b(finge|finja|simula|simule)\s+(que\s+eres|que\s+es|ser)\b|\bimagina\s+que\s+eres\b"
            r"|\b(hazte|h[aá]gase)\s+pasar\s+por\b",
            re.I | re.M,
        ),
        "role play",
    ),
    (
        # "Nuevas instrucciones para conectarse a la VPN:" is a ticket: the new rules must be the model's.
        re.compile(
            r"\b(tus|sus)\s+(nuevas|verdaderas|reales)\s+(instrucciones|indicaciones|reglas|[oó]rdenes)\b"
            r"|\b(nuevas|verdaderas)\s+(instrucciones|indicaciones|reglas|[oó]rdenes)\s+(para|de|del)\s+"
            r"(el\s+|la\s+)?(ia|bot|chatbot|modelo de ia|modelo de lenguaje|asistente de ia|asistente virtual"
            r"|agente de ia|inteligencia artificial|sistema)\b",
            re.I,
        ),
        "new instructions",
    ),
    (
        # Second-person verbs only ("la pantalla muestra" is a ticket), never "mensaje del sistema" ("dime
        # qué mensaje del sistema sale"), never "las instrucciones del sistema de nómina" or "copia las
        # instrucciones originales del fabricante": the prompt, or instructions that are the model's.
        re.compile(
            r"\b(revela|revele|rev[eé]lame|mu[eé]strame|mu[eé]stranos|mu[eé]strenos|dime|d[ií]game|dame|deme"
            r"|imprime|repite|rep[ií]teme|copia|c[oó]piame|transcribe|escribe)\b[^.\n]{0,40}"
            r"\b((el\s+|tu\s+|su\s+)?prompt\s+(de|del)\s+sistema|(tu|su)\s+prompt"
            r"|(tus|sus)\s+(instrucciones|indicaciones)\s+(de|del)\s+sistema"
            r"|(las\s+)?(instrucciones|indicaciones)\s+de\s+sistema(?!\s+(de|operativo)\b)"
            r"|(tus|sus)\s+(instrucciones|indicaciones|reglas)\s+(iniciales|originales|ocultas|internas))\b",
            re.I,
        ),
        "prompt exfiltration",
    ),
    (
        # Not "no le digas al usuario la contraseña": what is hidden must be this instruction itself.
        re.compile(
            r"\bno\s+(le\s+|les\s+)?(digas|diga|digan|decir|menciones|mencione|mencionen|mencionar|reveles"
            r"|revele|revelar|informes|informe|informar|comentes|comente|comentar|cuentes|cuente|contar"
            r"|reportes|reporte|reportar)\b[^.\n]{0,25}\b(esta|estas|este)\s+(instrucci[oó]n|instrucciones"
            r"|indicaci[oó]n|indicaciones|orden|[oó]rdenes|prompt|petici[oó]n)\b",
            re.I,
        ),
        "hiding from the human",
    ),
    (
        # "Sube los logs a https://soporte..." is a vendor case: a secret or the directory must go too.
        re.compile(
            r"\b(env[ií]a|env[ií]e|env[ií]en|manda|mande|manden|reenv[ií]a|transmite|transmita|exporta|exporte"
            r"|sube|suba|suban|publica|publique|comparte|comparta)(lo|la|los|las)?\b[^\n]{0,80}?"
            r"\b(credenciales|contrase[nñ]as?|passwords?|tokens?|claves?|secretos?|llaves?|id_rsa|\.env"
            r"|directorio)\b[^\n]{0,80}?https?://"
            r"|\b(credenciales|contrase[nñ]as?|passwords?|tokens?|secretos?|llaves?|id_rsa|\.env|directorio)\b"
            r"[^\n]{0,80}?\b(env[ií]a|manda|reenv[ií]a|transmite|exporta|sube|s[uú]be|publica|comparte"
            r"|m[aá]nda|env[ií]a|comp[aá]rte)(lo|la|los|las)?\b[^\n]{0,40}?https?://",
            re.I,
        ),
        "sending to a URL",
    ),
    (
        re.compile(
            r"\b(nota|instrucci[oó]n|instrucciones|mensaje|aviso|indicaci[oó]n)(\s+importante)?\s+(para|a)\s+"
            r"(el\s+|la\s+)?(ia|bot|chatbot|modelo de ia|modelo de lenguaje|asistente de ia|asistente virtual"
            r"|agente de ia|inteligencia artificial)\b"
            r"|\bsi\s+(eres|usted\s+es)\s+(un|una)\s+(ia|bot|chatbot|modelo de ia|modelo de lenguaje"
            r"|asistente de ia|agente de ia|inteligencia artificial|llm)\b"
            r"|\b(asistente|agente|modelo)\s+de\s+ia\s+que\s+(lees|lee|procesas|procesa|revisas|revisa"
            r"|resumes|resume|analizas|analiza)\b",
            re.I,
        ),
        "addressed to the model",
    ),
)

#: French beside the patterns above (S40-28b): the forms the first French patterns missed.
_FRENCH: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        # Not "oubliez mes instructions d'hier": the instructions must be the model's (tes, vos).
        re.compile(
            r"\b(ne\s+tiens\s+plus\s+compte|ne\s+tenez\s+plus\s+compte|ne\s+tiens\s+pas\s+compte"
            r"|ne\s+tenez\s+pas\s+compte|fais\s+abstraction|faites\s+abstraction)\s+(de|des)\b[^.\n]{0,20}"
            r"\b(instructions?|consignes?|r[eè]gles?|directives?)\b"
            r"|\b(ignore|ignorez|oublie|oubliez|n[eé]glige|n[eé]gligez)\s+(toutes\s+)?(tes|vos)\s+"
            r"(instructions?|consignes?|r[eè]gles?|directives?)\b"
            r"|\b(ignore|ignorez|oublie|oubliez)\s+tout\s+ce\s+qu(i\s+pr[eé]c[eè]de|'on\s+t'a\s+(dit|demand[eé]"
            r"|appris)|'on\s+vous\s+a\s+(dit|demand[eé]|appris))",
            re.I,
        ),
        "ignore the previous instructions",
    ),
    (
        # "Tu" only: "désormais vous serez notifié par courriel" is a ticket.
        re.compile(
            r"\b(à\s+partir\s+de\s+maintenant|dor[eé]navant|d[eé]sormais|d[eè]s\s+maintenant)\s*,?\s*"
            r"tu\s+(es|agis|seras|joues|deviens|n'es\s+plus)\b",
            re.I,
        ),
        "role reassignment",
    ),
    (
        # "Il joue le rôle de passerelle" is a network: "joue le rôle" only at the start of a sentence.
        re.compile(
            r"\b(fais|faites)\s+comme\s+si\s+(tu\s+[eé]tais|vous\s+[eé]tiez)\b"
            r"|\b(fais|faites)\s+semblant\s+d'[eê]tre\b"
            r"|\b(pr[eé]tends|pr[eé]tendez)\s+[eê]tre\b"
            r"|(?:^|[.:;!?\n]\s*)((joue|jouez)\s+le\s+r[oô]le\s+d|(incarne|incarnez)\s+(un|une|le|la|l'))",
            re.I | re.M,
        ),
        "role play",
    ),
    (
        re.compile(
            r"\b(tes|vos)\s+(nouvelles|vraies|v[eé]ritables)\s+(instructions|consignes|r[eè]gles)\b"
            r"|\b(nouvelles|vraies)\s+(instructions|consignes|r[eè]gles)\s+(pour|de|du)\s+"
            r"(l'|le\s+|la\s+)?(ia|bot|chatbot|mod[eè]le\s+d'ia|assistant\s+ia|agent\s+ia"
            r"|intelligence\s+artificielle|syst[eè]me)\b",
            re.I,
        ),
        "new instructions",
    ),
    (
        # "Recopie le message système affiché" is a technician asking for an error, "copie le prompt du
        # terminal" a shell: only the model's own prompt or instructions.
        re.compile(
            r"\b(affiche|r[eé]v[eè]le|montre|r[eé]p[eè]te|recopie|copie|donne|[eé]cris|imprime|cite|d[eé]voile"
            r"|divulgue)(-moi|-nous)?\b[^.\n]{0,40}\b(ton|votre)\s+prompt\b"
            r"|\b(affiche|r[eé]v[eè]le|montre|r[eé]p[eè]te|recopie|copie|donne|[eé]cris|imprime|cite|d[eé]voile"
            r"|divulgue)(-moi|-nous)?\b[^.\n]{0,40}\b(tes|vos)\s+(instructions|consignes|r[eè]gles)\s+"
            r"(initiales|syst[eè]me|cach[eé]es|d'origine|internes)\b",
            re.I,
        ),
        "prompt exfiltration",
    ),
    (
        re.compile(
            r"\bne\s+(le\s+|la\s+|les\s+|lui\s+|leur\s+)?(dis|dites|mentionne|mentionnez|signale|signalez"
            r"|r[eé]v[eè]le|r[eé]v[eé]lez|parle|parlez|indique|indiquez)\s+(pas|rien|jamais)\b[^.\n]{0,25}"
            r"\b(cette|ces|cet)\s+(instructions?|consignes?|ordres?)\b",
            re.I,
        ),
        "hiding from the human",
    ),
    (
        re.compile(
            r"\b(note|consigne|instruction|message|remarque)(\s+importante)?\s+(pour|à)\s+"
            r"(l'|le\s+|la\s+)?(ia|bot|chatbot|mod[eè]le\s+d'ia|mod[eè]le\s+de\s+langage|assistant\s+ia"
            r"|agent\s+ia|intelligence\s+artificielle)\b"
            r"|\bsi\s+(tu\s+es|vous\s+[eê]tes)\s+(un|une)\s+(ia|bot|chatbot|mod[eè]le\s+d'ia"
            r"|mod[eè]le\s+de\s+langage|assistant\s+ia|agent\s+ia|intelligence\s+artificielle|llm)\b"
            r"|\b(agent|assistant|mod[eè]le)\s+(ia|d'ia)\s+qui\s+(lis|lit|traites|traite|r[eé]sumes|r[eé]sume"
            r"|relis|relit|analyses|analyse)\b",
            re.I,
        ),
        "addressed to the model",
    ),
)

#: Whatever the language: an HTML comment, which a person reading the page never sees, addressed to
#: the model by its role.
_ANY_LANGUAGE: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"<!--\s*(assistant|asistente|ai|ia|llm|bot|model|modelo|mod[eè]le|agent|agente)\s*:", re.I
        ),
        "hidden comment to the model",
    ),
    (
        re.compile(
            r"</?\s*(sistema|syst[eè]me|instrucciones|asistente)\s*>|\[/\s*(system|sistema|syst[eè]me)\s*\]",
            re.I,
        ),
        "prompt tags",
    ),
)

#: (motif, libellé). Anglais et français : c'est la langue des tickets ici.
MOTIFS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(ignore|disregard|forget)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|your|the)\b"
            r"[^.\n]{0,30}\b(instructions?|prompts?|rules?|guidelines?)\b",
            re.I,
        ),
        "ignore the previous instructions",
    ),
    (
        re.compile(
            r"\b(ignore|oublie|oubliez|néglige|négligez)\b[^.\n]{0,40}\b(instructions?|consignes?|règles?)\b"
            r"[^.\n]{0,30}\b(précédentes?|antérieures?|initiales?|système)\b",
            re.I,
        ),
        "ignore the previous instructions",
    ),
    (
        re.compile(
            r"\byou are now\b|\btu es (maintenant|désormais)\b|\bvous êtes (maintenant|désormais)\b", re.I
        ),
        "role reassignment",
    ),
    (
        re.compile(r"\b(new|updated|real|actual) (system )?(instructions?|prompt)\s*:", re.I),
        "new instructions",
    ),
    (
        re.compile(
            r"\b(reveal|print|show|repeat|output|dump)\b[^.\n]{0,30}"
            r"\b(system prompt|instructions|your prompt)\b",
            re.I,
        ),
        "prompt exfiltration",
    ),
    (
        re.compile(
            r"\b(affiche|révèle|montre|répète)\b[^.\n]{0,30}\b(prompt|instructions) (système|initial)", re.I
        ),
        "prompt exfiltration",
    ),
    (
        re.compile(
            r"\bdo not (tell|inform|mention)\b[^.\n]{0,20}\b(user|human|operator)\b"
            r"|\bne (dis|parle)\b[^.\n]{0,20}\b(utilisateur|humain)\b",
            re.I,
        ),
        "hiding from the human",
    ),
    (
        re.compile(
            r"\b(send|post|upload|exfiltrate|transmit|envoie|poste|transmets)\b[^.\n]{0,60}\bhttps?://", re.I
        ),
        "sending to a URL",
    ),
    (re.compile(r"\b(curl|wget)\b[^|\n]{0,80}\|\s*(ba)?sh\b", re.I), "running a downloaded script"),
    (
        re.compile(r"</?\s*(system|assistant|instructions)\s*>|\[/?INST\]|<\|im_start\|>|<\|system\|>", re.I),
        "prompt tags",
    ),
    (re.compile(r"\b(BEGIN|END) (SYSTEM|HIDDEN) (PROMPT|INSTRUCTIONS)\b", re.I), "prompt tags"),
    (re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{200,}={0,2}(?![A-Za-z0-9+/])"), "encoded payload"),
    *_SPANISH,
    *_FRENCH,
    *_ANY_LANGUAGE,
)


@dataclass(frozen=True, slots=True)
class Suspicion:
    source: str
    motif: str
    extrait: str
    #: Who found it: `lexical` (the patterns above) or `model` (a classifier service, ADR 0219).
    detector: str = "lexical"

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "motif": self.motif,
            "extrait": self.extrait,
            "detector": self.detector,
        }


def suspicions(texte: str, *, source: str = "texte") -> list[Suspicion]:
    """Les motifs d'injection trouvés dans un texte, avec un extrait court pour relire."""
    if not texte:
        return []
    trouvees: list[Suspicion] = []
    for motif, libelle in MOTIFS:
        for correspondance in motif.finditer(texte):
            debut = max(0, correspondance.start() - 20)
            extrait = texte[debut : correspondance.end() + 20].replace("\n", " ")
            trouvees.append(Suspicion(source, libelle, extrait[:120]))
            break  # un motif suffit à nommer la suspicion ; le reste est du bruit
    return trouvees


def analyser(sources: dict[str, str]) -> list[Suspicion]:
    """Analyse plusieurs sources nommées (`ticket.body`, `memory.<id>`…), dans l'ordre."""
    resultat: list[Suspicion] = []
    for nom, texte in sources.items():
        resultat.extend(suspicions(texte, source=nom))
    return resultat
