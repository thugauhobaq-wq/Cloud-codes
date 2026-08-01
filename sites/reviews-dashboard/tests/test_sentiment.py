import unittest

from reviews import sentiment as st


class SentimentTest(unittest.TestCase):
    def test_positive_text(self):
        result = st.analyze("Отличное место, персонал вежливый. Всем рекомендую!")
        self.assertEqual(result.label, "positive")
        self.assertGreater(result.score, 0.3)

    def test_negative_text(self):
        result = st.analyze("Ужасное обслуживание, сотрудник нахамил и отказался помогать.")
        self.assertEqual(result.label, "negative")
        self.assertLess(result.score, -0.3)

    def test_neutral_text(self):
        result = st.analyze("Обычное место, ничего особенного.")
        self.assertEqual(result.label, "neutral")

    def test_negation_flips_sign(self):
        positive = st.analyze("Мне понравилось")
        negated = st.analyze("Мне не понравилось")
        self.assertGreater(positive.score, 0)
        self.assertLess(negated.score, 0)

    def test_prefixed_verb_is_found(self):
        # «нахамил» = приставка «на» + основа «хам»
        self.assertLess(st.analyze("Продавец нахамил").score, 0)

    def test_intensifier_amplifies(self):
        plain = st.analyze("Хорошее кафе")
        strong = st.analyze("Очень хорошее кафе")
        self.assertGreater(strong.score, plain.score)

    def test_but_clause_dominates(self):
        result = st.analyze("Еда вкусная, но обслуживание ужасное")
        self.assertEqual(result.label, "negative")

    def test_rating_is_blended_in(self):
        low = st.analyze("Нормально", rating=1)
        high = st.analyze("Нормально", rating=5)
        self.assertLess(low.score, 0)
        self.assertGreater(high.score, 0)

    def test_emoji_counts(self):
        self.assertGreater(st.analyze("Всё супер 😊👍").score, 0)
        self.assertLess(st.analyze("Отвратительно 😡").score, 0)

    def test_aspects_use_context_not_only_rated_words(self):
        result = st.analyze("Доставка опоздала на неделю, курьер нагрубил")
        self.assertIn("Доставка", result.aspects)
        self.assertLess(result.aspects["Доставка"], 0)

    def test_aspect_positive_context(self):
        result = st.analyze("Цена очень приятная, дешевле чем везде")
        self.assertGreater(result.aspects.get("Цена", 0), 0)

    def test_empty_text_is_neutral(self):
        result = st.analyze("")
        self.assertEqual(result.label, "neutral")
        self.assertEqual(result.hits, 0)

    def test_score_within_bounds(self):
        text = "ужасно " * 50
        self.assertGreaterEqual(st.analyze(text).score, -1.0)
        self.assertLessEqual(st.analyze("отлично " * 50).score, 1.0)

    def test_label_ru(self):
        self.assertEqual(st.label_ru("positive"), "Позитив")
        self.assertEqual(st.label_ru("negative"), "Негатив")
        self.assertEqual(st.label_ru("neutral"), "Нейтрально")


if __name__ == "__main__":
    unittest.main()
