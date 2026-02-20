"""
Локализованные промпты для Claude API.

Единый источник истины для всех промптов, используемых при генерации контента.
Используется в:
- clients/claude.py (State Machine)
- bot.py (legacy, переходный период)
"""

from typing import Dict, Any

# Поддерживаемые языки
SUPPORTED_LANGUAGES = ['en', 'es', 'fr', 'zh', 'ru']
DEFAULT_LANGUAGE = 'ru'


def get_content_prompts(lang: str, study_duration: int, words: int) -> Dict[str, str]:
    """
    Получить локализованные промпты для генерации теоретического контента.

    Args:
        lang: код языка (ru, en, es, fr)
        study_duration: время на изучение в минутах
        words: количество слов

    Returns:
        Словарь с локализованными строками промпта
    """
    max_chars = words * 6  # ~6 chars per word (Russian average incl. spaces)
    prompts = {
        'ru': {
            'lang_instruction': "ВАЖНО: Пиши ВСЁ на русском языке.",
            'create_text': f"Создай текст на {study_duration} минут чтения (~{words} слов, не более {max_chars} символов). Без заголовков, только абзацы.",
            'engaging': "Текст должен быть вовлекающим, с примерами из жизни читателя.",
            'forbidden_header': "СТРОГО ЗАПРЕЩЕНО:",
            'forbidden_questions': "- Добавлять вопросы в любом месте текста",
            'forbidden_headers': "- Использовать заголовки типа \"Вопрос:\", \"Вопрос для размышления:\" и т.п.",
            'forbidden_end': "- Заканчивать текст вопросом",
            'question_later': "Вопрос будет задан отдельно после текста.",
            'topic': "Тема",
            'main_concept': "Основное понятие",
            'related_concepts': "Связанные понятия",
            'pain_point': "Боль читателя",
            'key_insight': "Ключевой инсайт",
            'source': "Источник",
            'content_instruction': "ИНСТРУКЦИЯ ПО КОНТЕНТУ",
            'context_from': "КОНТЕКСТ ИЗ МАТЕРИАЛОВ AISYSTANT",
            'start_with': "Начни с признания боли читателя, затем раскрой тему и подведи к ключевому инсайту.",
            'use_context': "Опирайся на контекст, но адаптируй под профиль стажера. Актуальные посты важнее.",
            'error_generation': "Не удалось сгенерировать контент. Попробуйте /learn ещё раз.",
        },
        'en': {
            'lang_instruction': "IMPORTANT: Write EVERYTHING in English.",
            'create_text': f"Create a text for {study_duration} minutes of reading (~{words} words, max {max_chars} characters). No headings, only paragraphs.",
            'engaging': "The text should be engaging, with examples from the reader's life.",
            'forbidden_header': "STRICTLY FORBIDDEN:",
            'forbidden_questions': "- Adding questions anywhere in the text",
            'forbidden_headers': "- Using headers like \"Question:\", \"Question for reflection:\" etc.",
            'forbidden_end': "- Ending the text with a question",
            'question_later': "A question will be asked separately after the text.",
            'topic': "Topic",
            'main_concept': "Main concept",
            'related_concepts': "Related concepts",
            'pain_point': "Reader's pain",
            'key_insight': "Key insight",
            'source': "Source",
            'content_instruction': "CONTENT INSTRUCTION",
            'context_from': "CONTEXT FROM AISYSTANT MATERIALS",
            'start_with': "Start by acknowledging the reader's pain, then develop the topic and lead to the key insight.",
            'use_context': "Use the context, but adapt it to the student's profile. Recent posts are more important.",
            'error_generation': "Failed to generate content. Please try /learn again.",
        },
        'es': {
            'lang_instruction': "IMPORTANTE: Escribe TODO en español.",
            'create_text': f"Crea un texto para {study_duration} minutos de lectura (~{words} palabras, máximo {max_chars} caracteres). Sin títulos, solo párrafos.",
            'engaging': "El texto debe ser atractivo, con ejemplos de la vida del lector.",
            'forbidden_header': "ESTRICTAMENTE PROHIBIDO:",
            'forbidden_questions': "- Agregar preguntas en cualquier parte del texto",
            'forbidden_headers': "- Usar encabezados como \"Pregunta:\", \"Pregunta para reflexionar:\" etc.",
            'forbidden_end': "- Terminar el texto con una pregunta",
            'question_later': "Se hará una pregunta por separado después del texto.",
            'topic': "Tema",
            'main_concept': "Concepto principal",
            'related_concepts': "Conceptos relacionados",
            'pain_point': "Dolor del lector",
            'key_insight': "Idea clave",
            'source': "Fuente",
            'content_instruction': "INSTRUCCIÓN DE CONTENIDO",
            'context_from': "CONTEXTO DE MATERIALES AISYSTANT",
            'start_with': "Comienza reconociendo el dolor del lector, luego desarrolla el tema y lleva a la idea clave.",
            'use_context': "Usa el contexto, pero adáptalo al perfil del estudiante. Las publicaciones recientes son más importantes.",
            'error_generation': "No se pudo generar el contenido. Por favor, intente /learn de nuevo.",
        },
        'fr': {
            'lang_instruction': "IMPORTANT: Écris TOUT en français.",
            'create_text': f"Crée un texte pour {study_duration} minutes de lecture (~{words} mots, maximum {max_chars} caractères). Sans titres, seulement des paragraphes.",
            'engaging': "Le texte doit être engageant, avec des exemples de la vie du lecteur.",
            'forbidden_header': "STRICTEMENT INTERDIT:",
            'forbidden_questions': "- Ajouter des questions n'importe où dans le texte",
            'forbidden_headers': "- Utiliser des en-têtes comme \"Question:\", \"Question de réflexion:\" etc.",
            'forbidden_end': "- Terminer le texte par une question",
            'question_later': "Une question sera posée séparément après le texte.",
            'topic': "Sujet",
            'main_concept': "Concept principal",
            'related_concepts': "Concepts liés",
            'pain_point': "Douleur du lecteur",
            'key_insight': "Idée clé",
            'source': "Source",
            'content_instruction': "INSTRUCTION DE CONTENU",
            'context_from': "CONTEXTE DES MATÉRIAUX AISYSTANT",
            'start_with': "Commence par reconnaître la douleur du lecteur, puis développe le sujet et mène à l'idée clé.",
            'use_context': "Utilise le contexte, mais adapte-le au profil de l'étudiant. Les publications récentes sont plus importantes.",
            'error_generation': "Échec de la génération du contenu. Veuillez réessayer /learn.",
        },
        'zh': {
            'lang_instruction': "重要：请用中文书写所有内容。",
            'create_text': f"创建一篇{study_duration}分钟阅读量的文本（约{words}字，不超过{max_chars}字符）。不要标题，只用段落。",
            'engaging': "文本应具有吸引力，包含与读者生活相关的例子。",
            'forbidden_header': "严格禁止：",
            'forbidden_questions': "- 在文本任何位置添加问题",
            'forbidden_headers': '- 使用"问题："、"思考问题："等标题',
            'forbidden_end': "- 以问题结尾",
            'question_later': "问题将在文本之后单独提出。",
            'topic': "主题",
            'main_concept': "核心概念",
            'related_concepts': "相关概念",
            'pain_point': "读者的痛点",
            'key_insight': "关键洞察",
            'source': "来源",
            'content_instruction': "内容指导",
            'context_from': "AISYSTANT资料的上下文",
            'start_with': "先承认读者的痛点，然后展开主题并引向关键洞察。",
            'use_context': "参考上下文，但要适配学员的个人资料。最近的文章更为重要。",
            'error_generation': "无法生成内容。请再次尝试 /learn。",
        }
    }

    return prompts.get(lang, prompts[DEFAULT_LANGUAGE])


def get_practice_prompts(lang: str) -> Dict[str, str]:
    """
    Получить локализованные промпты для генерации практических заданий.

    Args:
        lang: код языка (ru, en, es, fr)

    Returns:
        Словарь с локализованными строками промпта
    """
    prompts = {
        'ru': {
            'lang_instruction': "ВАЖНО: Пиши ВСЁ на русском языке.",
            'task_header': "ЗАДАНИЕ",
            'work_product_header': "РАБОЧИЙ ПРОДУКТ",
            'examples_header': "ПРИМЕРЫ",
            'intro_instruction': "Напиши краткое вдохновляющее введение к заданию (2-3 предложения).",
            'task_instruction': "Переведи и адаптируй задание под профиль стажера.",
            'wp_instruction': "Сформулируй рабочий продукт. РП начинается с существительного, обозначающего ДОКУМЕНТ, ВЕЩЬ или СИСТЕМУ В ОПРЕДЕЛЁННОМ СОСТОЯНИИ (чек-лист, схема, таблица, текст, пост, описание, план, набор, реестр, система с настроенным X, бот с реализованной функцией Y). ЗАПРЕЩЕНО начинать с отглагольных существительных-процессов (анализ, исследование, обзор, диагностика, сравнение). Тест: можно ли это увидеть/передать/показать?",
            'examples_instruction': "Приведи 2-3 примера хороших рабочих продуктов.",
            'error_generation': "Не удалось сгенерировать задание. Попробуйте ещё раз.",
        },
        'en': {
            'lang_instruction': "IMPORTANT: Write EVERYTHING in English.",
            'task_header': "TASK",
            'work_product_header': "WORK PRODUCT",
            'examples_header': "EXAMPLES",
            'intro_instruction': "Write a brief inspiring introduction to the task (2-3 sentences).",
            'task_instruction': "Translate and adapt the task to the student's profile.",
            'wp_instruction': "Describe the expected work product.",
            'examples_instruction': "Provide 2-3 examples of good work products.",
            'error_generation': "Failed to generate the task. Please try again.",
        },
        'es': {
            'lang_instruction': "IMPORTANTE: Escribe TODO en español.",
            'task_header': "TAREA",
            'work_product_header': "PRODUCTO DE TRABAJO",
            'examples_header': "EJEMPLOS",
            'intro_instruction': "Escribe una breve introducción inspiradora a la tarea (2-3 oraciones).",
            'task_instruction': "Traduce y adapta la tarea al perfil del estudiante.",
            'wp_instruction': "Describe el producto de trabajo esperado.",
            'examples_instruction': "Proporciona 2-3 ejemplos de buenos productos de trabajo.",
            'error_generation': "No se pudo generar la tarea. Por favor, inténtelo de nuevo.",
        },
        'fr': {
            'lang_instruction': "IMPORTANT: Écris TOUT en français.",
            'task_header': "TÂCHE",
            'work_product_header': "PRODUIT DE TRAVAIL",
            'examples_header': "EXEMPLES",
            'intro_instruction': "Écris une brève introduction inspirante à la tâche (2-3 phrases).",
            'task_instruction': "Traduis et adapte la tâche au profil de l'étudiant.",
            'wp_instruction': "Décris le produit de travail attendu.",
            'examples_instruction': "Fournis 2-3 exemples de bons produits de travail.",
            'error_generation': "Échec de la génération de la tâche. Veuillez réessayer.",
        },
        'zh': {
            'lang_instruction': "重要：请用中文书写所有内容。",
            'task_header': "任务",
            'work_product_header': "工作成果",
            'examples_header': "示例",
            'intro_instruction': "写一段简短的任务介绍（2-3句话）。",
            'task_instruction': "根据学员的个人资料翻译并调整任务。",
            'wp_instruction': "描述预期的工作成果。",
            'examples_instruction': "提供2-3个优秀工作成果的示例。",
            'error_generation': "无法生成任务。请重试。",
        }
    }

    return prompts.get(lang, prompts[DEFAULT_LANGUAGE])


def get_question_prompts(lang: str) -> Dict[str, str]:
    """
    Получить локализованные промпты для генерации вопросов.

    Args:
        lang: код языка (ru, en, es, fr)

    Returns:
        Словарь с локализованными строками промпта
    """
    prompts = {
        'ru': {
            'lang_instruction': "ВАЖНО: Задай вопрос на русском языке.",
            'generate_question': "Сгенерируй один вопрос для проверки понимания темы.",
            'question_type_1': "Задай вопрос на РАЗЛИЧЕНИЕ понятий (\"В чём разница между...\", \"Чем отличается...\").",
            'question_type_2': "Задай ОТКРЫТЫЙ вопрос на понимание (\"Почему...\", \"Как вы понимаете...\", \"Объясните связь...\").",
            'question_type_3': "Задай вопрос на ПРИМЕНЕНИЕ и АНАЛИЗ (\"Приведите пример из жизни\", \"Проанализируйте ситуацию\", \"Как бы вы объяснили коллеге...\").",
            'forbidden_header': "СТРОГО ЗАПРЕЩЕНО:",
            'forbidden_intro': "- Писать введение, объяснения, контекст или любой текст перед вопросом",
            'forbidden_headers': "- Писать заголовки типа \"Вопрос:\", \"Вопрос для размышления:\" и т.п.",
            'forbidden_examples': "- Писать примеры, истории, мотивацию",
            'forbidden_after': "- Писать что-либо после вопроса",
            'only_question': "Выдай ТОЛЬКО сам вопрос — 1-3 предложения максимум.",
            'related_to_occupation': "Вопрос должен быть связан с профессией:",
            'complexity_level': "Уровень сложности:",
            'examples_hint': "ПРИМЕРЫ ВОПРОСОВ (используй как образец стиля):",
            'topic': "Тема",
            'concept': "Понятие",
            'output_only_question': "Выдай ТОЛЬКО вопрос (1-3 предложения), без введения и пояснений.",
            'no_numbering': "Не нумеруй вопрос, просто напиши его текст.",
            'error_generation': "Не удалось сгенерировать вопрос.",
        },
        'en': {
            'lang_instruction': "IMPORTANT: Ask the question in English.",
            'generate_question': "Generate one question to check understanding of the topic.",
            'question_type_1': "Ask a DISTINCTION question (\"What is the difference between...\", \"How does X differ from Y...\").",
            'question_type_2': "Ask an OPEN-ENDED comprehension question (\"Why...\", \"How do you understand...\", \"Explain the connection...\").",
            'question_type_3': "Ask an APPLICATION and ANALYSIS question (\"Give an example from your life\", \"Analyze the situation\", \"How would you explain to a colleague...\").",
            'forbidden_header': "STRICTLY FORBIDDEN:",
            'forbidden_intro': "- Writing introduction, explanations, context or any text before the question",
            'forbidden_headers': "- Writing headers like \"Question:\", \"Question for reflection:\" etc.",
            'forbidden_examples': "- Writing examples, stories, motivation",
            'forbidden_after': "- Writing anything after the question",
            'only_question': "Output ONLY the question itself — 1-3 sentences maximum.",
            'related_to_occupation': "The question should be related to the occupation:",
            'complexity_level': "Complexity level:",
            'examples_hint': "EXAMPLE QUESTIONS (use as style reference):",
            'topic': "Topic",
            'concept': "Concept",
            'output_only_question': "Output ONLY the question (1-3 sentences), without introduction or explanations.",
            'no_numbering': "Don't number the question, just write its text.",
            'error_generation': "Failed to generate the question.",
        },
        'es': {
            'lang_instruction': "IMPORTANTE: Haz la pregunta en español.",
            'generate_question': "Genera una pregunta para verificar la comprensión del tema.",
            'question_type_1': "Haz una pregunta de DISTINCIÓN (\"¿Cuál es la diferencia entre...\", \"¿En qué se diferencia...\").",
            'question_type_2': "Haz una pregunta ABIERTA de comprensión (\"¿Por qué...\", \"¿Cómo entiendes...\", \"Explica la conexión...\").",
            'question_type_3': "Haz una pregunta de APLICACIÓN y ANÁLISIS (\"Da un ejemplo de tu vida\", \"Analiza la situación\", \"¿Cómo le explicarías a un colega...\").",
            'forbidden_header': "ESTRICTAMENTE PROHIBIDO:",
            'forbidden_intro': "- Escribir introducción, explicaciones, contexto o cualquier texto antes de la pregunta",
            'forbidden_headers': "- Escribir encabezados como \"Pregunta:\", \"Pregunta para reflexionar:\" etc.",
            'forbidden_examples': "- Escribir ejemplos, historias, motivación",
            'forbidden_after': "- Escribir algo después de la pregunta",
            'only_question': "Genera SOLO la pregunta — 1-3 oraciones máximo.",
            'related_to_occupation': "La pregunta debe estar relacionada con la ocupación:",
            'complexity_level': "Nivel de complejidad:",
            'examples_hint': "EJEMPLOS DE PREGUNTAS (usa como referencia de estilo):",
            'topic': "Tema",
            'concept': "Concepto",
            'output_only_question': "Genera SOLO la pregunta (1-3 oraciones), sin introducción ni explicaciones.",
            'no_numbering': "No numeres la pregunta, solo escribe su texto.",
            'error_generation': "No se pudo generar la pregunta.",
        },
        'fr': {
            'lang_instruction': "IMPORTANT: Pose la question en français.",
            'generate_question': "Génère une question pour vérifier la compréhension du sujet.",
            'question_type_1': "Pose une question de DISTINCTION (\"Quelle est la différence entre...\", \"En quoi X diffère de Y...\").",
            'question_type_2': "Pose une question OUVERTE de compréhension (\"Pourquoi...\", \"Comment comprenez-vous...\", \"Expliquez le lien...\").",
            'question_type_3': "Pose une question d'APPLICATION et d'ANALYSE (\"Donne un exemple de ta vie\", \"Analyse la situation\", \"Comment l'expliquerais-tu à un collègue...\").",
            'forbidden_header': "STRICTEMENT INTERDIT:",
            'forbidden_intro': "- Écrire une introduction, des explications, du contexte ou tout texte avant la question",
            'forbidden_headers': "- Écrire des en-têtes comme \"Question:\", \"Question de réflexion:\" etc.",
            'forbidden_examples': "- Écrire des exemples, des histoires, de la motivation",
            'forbidden_after': "- Écrire quoi que ce soit après la question",
            'only_question': "Génère UNIQUEMENT la question elle-même — 1-3 phrases maximum.",
            'related_to_occupation': "La question doit être liée à la profession:",
            'complexity_level': "Niveau de complexité:",
            'examples_hint': "EXEMPLES DE QUESTIONS (utilise comme référence de style):",
            'topic': "Sujet",
            'concept': "Concept",
            'output_only_question': "Génère UNIQUEMENT la question (1-3 phrases), sans introduction ni explications.",
            'no_numbering': "Ne numérote pas la question, écris simplement son texte.",
            'error_generation': "Échec de la génération de la question.",
        },
        'zh': {
            'lang_instruction': "重要：请用中文提问。",
            'generate_question': "生成一个检验主题理解的问题。",
            'question_type_1': '提出一个区分概念的问题（"……和……有什么区别"、"……与……有何不同"）。',
            'question_type_2': '提出一个开放式理解问题（"为什么……"、"你如何理解……"、"解释……之间的联系"）。',
            'question_type_3': '提出一个应用与分析问题（"举一个生活中的例子"、"分析这个情况"、"你会如何向同事解释……"）。',
            'forbidden_header': "严格禁止：",
            'forbidden_intro': "- 在问题前写任何介绍、解释、背景或文字",
            'forbidden_headers': '- 使用"问题："、"思考问题："等标题',
            'forbidden_examples': "- 写示例、故事、鼓励性文字",
            'forbidden_after': "- 在问题后写任何内容",
            'only_question': "只输出问题本身——最多1-3句话。",
            'related_to_occupation': "问题应与职业相关：",
            'complexity_level': "难度级别：",
            'examples_hint': "问题示例（作为风格参考）：",
            'topic': "主题",
            'concept': "概念",
            'output_only_question': "只输出问题（1-3句话），不要介绍和解释。",
            'no_numbering': "不要给问题编号，直接写出问题文本。",
            'error_generation': "无法生成问题。",
        }
    }

    return prompts.get(lang, prompts[DEFAULT_LANGUAGE])


def get_feedback_prompts(lang: str) -> Dict[str, str]:
    """
    Получить локализованные промпты для генерации обратной связи.

    Args:
        lang: код языка (ru, en, es, fr)

    Returns:
        Словарь с локализованными строками промпта
    """
    prompts = {
        'ru': {
            'lang_instruction': "ВАЖНО: Пиши ВСЁ на русском языке.",
            'evaluate_answer': "Оцени ответ ученика на вопрос.",
            'be_supportive': "Будь поддерживающим, но честным.",
            'correct_answer': "Если ответ правильный, похвали и дополни.",
            'incorrect_answer': "Если ответ неправильный, мягко укажи на ошибку и объясни.",
            'partial_answer': "Если ответ частично правильный, отметь что верно и что можно улучшить.",
            'error_generation': "Не удалось сгенерировать обратную связь.",
        },
        'en': {
            'lang_instruction': "IMPORTANT: Write EVERYTHING in English.",
            'evaluate_answer': "Evaluate the student's answer to the question.",
            'be_supportive': "Be supportive but honest.",
            'correct_answer': "If the answer is correct, praise and add to it.",
            'incorrect_answer': "If the answer is incorrect, gently point out the error and explain.",
            'partial_answer': "If the answer is partially correct, note what's right and what can be improved.",
            'error_generation': "Failed to generate feedback.",
        },
        'es': {
            'lang_instruction': "IMPORTANTE: Escribe TODO en español.",
            'evaluate_answer': "Evalúa la respuesta del estudiante a la pregunta.",
            'be_supportive': "Sé comprensivo pero honesto.",
            'correct_answer': "Si la respuesta es correcta, elogia y complementa.",
            'incorrect_answer': "Si la respuesta es incorrecta, señala suavemente el error y explica.",
            'partial_answer': "Si la respuesta es parcialmente correcta, indica qué está bien y qué se puede mejorar.",
            'error_generation': "No se pudo generar la retroalimentación.",
        },
        'fr': {
            'lang_instruction': "IMPORTANT: Écris TOUT en français.",
            'evaluate_answer': "Évalue la réponse de l'étudiant à la question.",
            'be_supportive': "Sois encourageant mais honnête.",
            'correct_answer': "Si la réponse est correcte, félicite et complète.",
            'incorrect_answer': "Si la réponse est incorrecte, indique doucement l'erreur et explique.",
            'partial_answer': "Si la réponse est partiellement correcte, note ce qui est juste et ce qui peut être amélioré.",
            'error_generation': "Échec de la génération du feedback.",
        },
        'zh': {
            'lang_instruction': "重要：请用中文书写所有内容。",
            'evaluate_answer': "评估学员对问题的回答。",
            'be_supportive': "保持支持性但诚实。",
            'correct_answer': "如果回答正确，给予表扬并补充。",
            'incorrect_answer': "如果回答不正确，温和地指出错误并解释。",
            'partial_answer': "如果回答部分正确，指出正确之处和可以改进之处。",
            'error_generation': "无法生成反馈。",
        }
    }

    return prompts.get(lang, prompts[DEFAULT_LANGUAGE])


def get_consultation_prompts(lang: str) -> Dict[str, str]:
    """
    Получить локализованные промпты для консультаций (ответы на вопросы пользователя).

    Args:
        lang: код языка (ru, en, es, fr)

    Returns:
        Словарь с локализованными строками промпта
    """
    prompts = {
        'ru': {
            'lang_instruction': "ВАЖНО: Отвечай на русском языке.",
            'answer_question': "Ответь на вопрос пользователя по теме системного мышления и личного развития.",
            'use_context': "Используй контекст из материалов, если он релевантен.",
            'be_helpful': "Будь полезным и конкретным.",
            'admit_unknown': "Если не знаешь ответа, честно скажи об этом.",
            'error_generation': "Не удалось сгенерировать ответ на вопрос.",
        },
        'en': {
            'lang_instruction': "IMPORTANT: Answer in English.",
            'answer_question': "Answer the user's question about systems thinking and personal development.",
            'use_context': "Use context from the materials if relevant.",
            'be_helpful': "Be helpful and specific.",
            'admit_unknown': "If you don't know the answer, honestly say so.",
            'error_generation': "Failed to generate an answer to the question.",
        },
        'es': {
            'lang_instruction': "IMPORTANTE: Responde en español.",
            'answer_question': "Responde a la pregunta del usuario sobre pensamiento sistémico y desarrollo personal.",
            'use_context': "Usa el contexto de los materiales si es relevante.",
            'be_helpful': "Sé útil y específico.",
            'admit_unknown': "Si no sabes la respuesta, dilo honestamente.",
            'error_generation': "No se pudo generar una respuesta a la pregunta.",
        },
        'fr': {
            'lang_instruction': "IMPORTANT: Réponds en français.",
            'answer_question': "Réponds à la question de l'utilisateur sur la pensée systémique et le développement personnel.",
            'use_context': "Utilise le contexte des matériaux si pertinent.",
            'be_helpful': "Sois utile et précis.",
            'admit_unknown': "Si tu ne connais pas la réponse, dis-le honnêtement.",
            'error_generation': "Échec de la génération d'une réponse à la question.",
        },
        'zh': {
            'lang_instruction': "重要：请用中文回答。",
            'answer_question': "回答用户关于系统思维和个人发展的问题。",
            'use_context': "如果相关，请使用材料中的上下文。",
            'be_helpful': "保持有用和具体。",
            'admit_unknown': "如果你不知道答案，请诚实地说明。",
            'error_generation': "无法生成问题的答案。",
        }
    }

    return prompts.get(lang, prompts[DEFAULT_LANGUAGE])
