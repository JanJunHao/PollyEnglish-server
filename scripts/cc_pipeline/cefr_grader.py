#!/usr/bin/env python3
"""
CEFR Auto-Grader
================

基于多维度指标自动估算英文文本的 CEFR 等级（A1 / A2 / B1 / B2 / C1 / C2）。

核心思路：单一指标都不可靠，要组合 4 类信号：

  1. 可读性公式（Flesch, Flesch-Kincaid, Gunning Fog, Dale-Chall）
     → 这些公式本质上算的是"语法复杂度"
  2. 词汇分布（按 CEFR 词表对照）
     → 这是最强的难度信号，权重最高
  3. 平均句长
     → 长句多 = 更难
  4. 词长 / 难词比例
     → 多音节词、低频词比例

最终用加权和映射到 CEFR。词汇分布权重最高（0.45），因为它最能体现"对学习者
的实际难度"——一篇语法简单但满是专业术语的文章对 B1 学习者依然不可读。

依赖：
    pip install textstat

生产环境改进建议：
  - 用官方 Oxford 3000 / Oxford 5000 词表替换内置 fallback
  - 用 EVP (English Vocabulary Profile) 的 CEFR 词表更精准
  - 考虑词形还原 (lemmatization)，把 'running' 算到 'run' 上
  - 加入 spaCy 做语法复杂度分析（从句嵌套、被动语态等）
"""

import re
import logging
from pathlib import Path
from typing import Dict, List, Set, Optional, Any

import textstat

logger = logging.getLogger(__name__)


# =========================================================================
# 内置 CEFR 词表（fallback）
# 生产环境请替换为官方 Oxford 3000 / 5000 或 EVP 数据。
# 下面这些只是按使用频率挑选的代表性核心词，用于演示分级逻辑。
# =========================================================================

_A1_CORE = """
the be to of and a in that have I it for not on with he as you do at
this but his by from they we say her she or an will my one all would
there their what so up out if about who get which go me when make can
like time no just him know take people into year your good some could
them see other than then now look only come its over think also back
after use two how our work first well well way even new want because
any these give day most us is are was were been being has had having
am did does doing done very much many more less few every another same
yes hi hello bye thank thanks please morning night today tomorrow
yesterday week month home house school car bus train food water eat
drink sleep walk run sit stand buy sell big small old young high low
happy sad fast slow hot cold open close start stop come going went gone
mother father brother sister friend dog cat boy girl man woman child
red blue green yellow black white book pen door window chair table phone
one two three four five six seven eight nine ten
""".split()

_A2_ADD = """
believe build change create follow include leave begin feel show try ask
need become find tell help important different interesting difficult
possible available perhaps probably certainly actually completely exactly
develop produce remember consider continue improve experience education
government information situation national international natural personal
professional social special particular several whole though although while
during through against without across along among beyond beside between
toward inside outside upon despite within above below behind ahead nearby
remember understand explain decide accept reject prefer offer suggest mean
expect plan teach learn study practice listen speak read write travel
shopping cooking cleaning meeting checking sending arriving leaving
""".split()

_B1_ADD = """
achieve acknowledge adapt affect approach argue assume available consist
constitute context contrast convince demonstrate determine distinguish
emerge emphasize ensure establish evaluate evident examine exhibit expand
phenomenon principle procedure process research significant structure
theory tradition variety according regardless meanwhile moreover therefore
consequently nevertheless similarly otherwise specifically generally
typically ultimately essentially relatively presumably accordingly hence
particularly notably arguably interestingly fortunately unfortunately
appropriate equivalent intermediate substantial sufficient considerable
recognize represent require respond involve maintain promote provide
reduce remove replace report require result reveal support suggest
indicate identify illustrate suspect estimate analyze approve prefer
""".split()

_B2_ADD = """
abstract accommodate accompany accumulate adjacent ambiguous anticipate
arbitrary circumstance coherent collaborate comprehensive contradict
controversy deteriorate discrepancy elaborate encompass fluctuate
fundamental implicit incompatible inevitable intervene manipulate ongoing
paradigm preliminary predominantly presumption reciprocal rigorous
sophisticated subsequent underlying unprecedented allegedly inherently
exclusively explicitly notwithstanding henceforth thereby thereof whereby
albeit despite furthermore consequently nonetheless conversely respectively
proponent advocate skeptic critic adversary contender bureaucracy ideology
infrastructure jurisdiction legitimacy methodology terminology trajectory
sustainability accountability transparency vulnerability susceptibility
deteriorate exacerbate mitigate alleviate undermine attain forge confront
""".split()


def _wordset(*lists) -> Set[str]:
    """合并多个词列表，统一小写。"""
    result = set()
    for lst in lists:
        result.update(w.lower() for w in lst)
    return result


# 累积式：A2 包含 A1，B1 包含 A1+A2，依此类推
_DEFAULT_VOCAB = {
    'A1': _wordset(_A1_CORE),
    'A2': _wordset(_A1_CORE, _A2_ADD),
    'B1': _wordset(_A1_CORE, _A2_ADD, _B1_ADD),
    'B2': _wordset(_A1_CORE, _A2_ADD, _B1_ADD, _B2_ADD),
}


# =========================================================================
# 评分器主类
# =========================================================================


class CEFRGrader:
    """多维度 CEFR 自动评级器。"""

    def __init__(self, vocab_lists: Optional[Dict[str, Set[str]]] = None):
        """
        Args:
            vocab_lists: 自定义 CEFR 词表（A1/A2/B1/B2 → set of words）。
                生产环境强烈建议传入 Oxford 3000/5000 或 EVP 数据。
        """
        self.vocab = vocab_lists or _DEFAULT_VOCAB

    # ----------------- 维度 1: 词汇难度 -----------------

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """简单小写分词（生产环境可换 spaCy 做 lemmatization）。"""
        return re.findall(r"[a-zA-Z']+", text.lower())

    def vocab_difficulty(self, text: str) -> Dict[str, float]:
        """按 CEFR 词表对照，统计每个等级的词汇占比。

        返回的 difficulty 是 1-5 的加权分数（A1=1, A2=2, B1=3, B2=4, 未知=5）。
        """
        tokens = self._tokenize(text)
        if not tokens:
            return {'a1_pct': 0, 'a2_pct': 0, 'b1_pct': 0, 'b2_pct': 0,
                    'unknown_pct': 0, 'difficulty': 0}

        counts = {'A1': 0, 'A2': 0, 'B1': 0, 'B2': 0, 'unknown': 0}
        for tok in tokens:
            if tok in self.vocab['A1']:
                counts['A1'] += 1
            elif tok in self.vocab['A2']:
                counts['A2'] += 1
            elif tok in self.vocab['B1']:
                counts['B1'] += 1
            elif tok in self.vocab['B2']:
                counts['B2'] += 1
            else:
                counts['unknown'] += 1

        total = len(tokens)
        pct = {k: v / total * 100 for k, v in counts.items()}
        difficulty = (
            counts['A1'] * 1 + counts['A2'] * 2 +
            counts['B1'] * 3 + counts['B2'] * 4 +
            counts['unknown'] * 5
        ) / total

        return {
            'a1_pct': round(pct['A1'], 1),
            'a2_pct': round(pct['A2'], 1),
            'b1_pct': round(pct['B1'], 1),
            'b2_pct': round(pct['B2'], 1),
            'unknown_pct': round(pct['unknown'], 1),
            'difficulty': round(difficulty, 2),
            'total_tokens': total,
            'unique_tokens': len(set(tokens)),
            'type_token_ratio': round(len(set(tokens)) / total, 3),
        }

    # ----------------- 维度 2: 可读性公式 -----------------

    @staticmethod
    def readability_metrics(text: str) -> Dict[str, float]:
        """调用 textstat 计算标准可读性指标。"""
        try:
            return {
                'flesch_reading_ease': textstat.flesch_reading_ease(text),
                'flesch_kincaid_grade': textstat.flesch_kincaid_grade(text),
                'gunning_fog': textstat.gunning_fog(text),
                'smog_index': textstat.smog_index(text),
                'automated_readability_index':
                    textstat.automated_readability_index(text),
                'dale_chall': textstat.dale_chall_readability_score(text),
                'avg_sentence_length':
                    textstat.avg_sentence_length(text),
                'avg_letters_per_word':
                    textstat.avg_letter_per_word(text),
                'difficult_words_pct': (
                    textstat.difficult_words(text) /
                    max(textstat.lexicon_count(text), 1) * 100
                ),
                'syllable_count': textstat.syllable_count(text),
                'lexicon_count': textstat.lexicon_count(text),
                'sentence_count': textstat.sentence_count(text),
            }
        except Exception as e:
            logger.warning(f"Readability calc failed: {e}")
            return {}

    # ----------------- 综合评分 -----------------

    def grade(self, text: str) -> Dict[str, Any]:
        """完整评级流程，返回 CEFR 等级 + 各维度子分数。"""
        if not text or len(text.split()) < 30:
            return {
                'cefr': 'UNKNOWN',
                'composite_score': 0,
                'error': 'Text too short (min 30 words required)',
            }

        vocab = self.vocab_difficulty(text)
        read = self.readability_metrics(text)

        # 子分数：每个维度都规整到 1-6 量纲
        # （1=A1 最简单, 2=A2, 3=B1, 4=B2, 5=C1, 6=C2 最难）

        # 1) Flesch Reading Ease：越高越简单
        flesch = read.get('flesch_reading_ease', 50)
        if   flesch >= 90: flesch_s = 1
        elif flesch >= 80: flesch_s = 2
        elif flesch >= 70: flesch_s = 3
        elif flesch >= 60: flesch_s = 4
        elif flesch >= 50: flesch_s = 5
        else:              flesch_s = 6

        # 2) Flesch-Kincaid 美国年级
        fk = read.get('flesch_kincaid_grade', 8)
        if   fk <= 3:  fk_s = 1
        elif fk <= 5:  fk_s = 2
        elif fk <= 7:  fk_s = 3
        elif fk <= 10: fk_s = 4
        elif fk <= 13: fk_s = 5
        else:          fk_s = 6

        # 3) 词汇难度（已经是 1-5，略微拉伸到 1-6）
        vocab_s = min(6.0, vocab['difficulty'] * 1.2)

        # 4) 平均句长
        asl = read.get('avg_sentence_length', 15)
        if   asl <= 8:  asl_s = 1
        elif asl <= 12: asl_s = 2
        elif asl <= 16: asl_s = 3
        elif asl <= 20: asl_s = 4
        elif asl <= 25: asl_s = 5
        else:           asl_s = 6

        # 5) 难词比例（textstat 的 difficult_words 大致对应不在 Dale-Chall 列表的词）
        dw_pct = read.get('difficult_words_pct', 10)
        if   dw_pct <= 3:  dw_s = 1
        elif dw_pct <= 7:  dw_s = 2
        elif dw_pct <= 12: dw_s = 3
        elif dw_pct <= 18: dw_s = 4
        elif dw_pct <= 25: dw_s = 5
        else:              dw_s = 6

        # 加权汇总（词汇权重最高，因为它最能反映学习者的真实难度）
        composite = (
            flesch_s * 0.15 +
            fk_s     * 0.15 +
            vocab_s  * 0.40 +
            asl_s    * 0.15 +
            dw_s     * 0.15
        )

        cefr = self._composite_to_cefr(composite)

        return {
            'cefr': cefr,
            'composite_score': round(composite, 2),
            'sub_scores': {
                'flesch':           round(flesch_s, 2),
                'fk_grade':         round(fk_s, 2),
                'vocab':            round(vocab_s, 2),
                'sentence_length':  round(asl_s, 2),
                'difficult_words':  round(dw_s, 2),
            },
            'vocab_metrics': vocab,
            'readability_metrics': read,
        }

    @staticmethod
    def _composite_to_cefr(score: float) -> str:
        """1-6 量纲 → A1-C2 等级。"""
        if score <= 1.5: return 'A1'
        if score <= 2.5: return 'A2'
        if score <= 3.5: return 'B1'
        if score <= 4.5: return 'B2'
        if score <= 5.5: return 'C1'
        return 'C2'


# =========================================================================
# 外部数据加载
# =========================================================================

def load_wordlist_file(path: str) -> Set[str]:
    """加载词表文件（每行一个词），用于替换内置 fallback。

    推荐数据源：
      - Oxford 3000 / 5000: https://www.oxfordlearnersdictionaries.com/wordlists/
      - English Vocabulary Profile (EVP): https://www.englishprofile.org/
      - NGSL (New General Service List): http://www.newgeneralservicelist.org/
    """
    return {
        line.strip().lower()
        for line in Path(path).read_text(encoding='utf-8').splitlines()
        if line.strip() and not line.strip().startswith('#')
    }


def load_cefr_vocab(
    a1_path: str,
    a2_path: str,
    b1_path: str,
    b2_path: str,
) -> Dict[str, Set[str]]:
    """从文件加载完整 CEFR 词表（生产环境推荐）。"""
    a1 = load_wordlist_file(a1_path)
    a2 = load_wordlist_file(a2_path) | a1
    b1 = load_wordlist_file(b1_path) | a2
    b2 = load_wordlist_file(b2_path) | b1
    return {'A1': a1, 'A2': a2, 'B1': b1, 'B2': b2}


# =========================================================================
# Demo
# =========================================================================

if __name__ == '__main__':
    grader = CEFRGrader()

    samples = {
        'A1 sample': (
            "Hello. My name is Tom. I am ten years old. I live in a big city. "
            "I have a small dog. The dog is white. I like to play with my dog "
            "every day after school. My mother and father are very nice. We "
            "eat dinner together. I love my family. I am very happy."
        ),
        'B1 sample': (
            "Climate change is one of the most important issues facing the "
            "world today. Scientists have shown that human activities are the "
            "main cause of rising temperatures. We burn coal, oil and gas, "
            "which releases carbon dioxide into the atmosphere. This gas traps "
            "heat and makes the planet warmer. We need to find practical ways "
            "to reduce our impact on the environment before it is too late."
        ),
        'C1 sample': (
            "The intricate interplay between socioeconomic circumstances and "
            "neurobiological development has prompted contemporary researchers "
            "to reconsider longstanding assumptions about cognitive maturation. "
            "Recent findings suggest that environmental stressors may exert "
            "profound, often irreversible effects on neural plasticity, "
            "particularly during critical developmental windows. Such "
            "discoveries underscore the necessity of comprehensive interventions "
            "that address both physiological and contextual dimensions of "
            "early childhood adversity."
        ),
    }

    print("=" * 70)
    for label, text in samples.items():
        result = grader.grade(text)
        print(f"\n{label}")
        print(f"  Estimated CEFR : {result['cefr']}")
        print(f"  Composite score: {result['composite_score']} / 6")
        print(f"  Sub-scores     : {result['sub_scores']}")
        print(f"  Flesch         : {result['readability_metrics']['flesch_reading_ease']:.1f}")
        print(f"  Vocab unknown %: {result['vocab_metrics']['unknown_pct']}%")
    print("=" * 70)
