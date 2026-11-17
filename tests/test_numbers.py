from __future__ import annotations

import unittest

import support  # noqa: F401
from douyin_text import numbers


class NumberTests(unittest.TestCase):
    def test_measurements_statistics_and_real_excerpts(self):
        cases = {
            '十五天': '15 天', '一百八十二毫克': '182 毫克', '三千五百人': '3500 人',
            '三公里': '三公里', '五克': '五克', '三千克': '三千克', '五千米': '五千米',
            '十个': '10 个',
            '两百零五毫米': '205 毫米', '一千零一人': '1001 人', '十一个人': '11 个人',
            '三点五公斤': '3.5 公斤', '零点二毫升': '0.2 毫升',
            '减肥法，十五天掉二十个，你敢信吗': '减肥法，15 天掉 20 个，你敢信吗',
            '只有一百八十二毫克': '只有 182 毫克', '腾讯的十八个智能体': '腾讯的 18 个智能体',
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(expected, numbers.apply(raw)[0])

    def test_years_percent_dates_models_scores_and_large_numbers(self):
        cases = {
            '一九八七年': '1987 年', '二零二六年': '2026 年', '二〇二六年': '2026 年', '二零五一年': '2051 年',
            '百分之三十': '30%', '百分之三点五': '3.5%', '占百分之三点五的人': '占 3.5% 的人',
            '十一月一号': '11 月 1 号', '二月二十九日': '2 月 29 日', '八月十五日': '8 月 15 日',
            'M四A一': 'M4A1', 'AK四七': 'AK47', 'AK五一': 'AK51', '型号X一百八十二': '型号 X182', 'T三十四': 'T34',
            '比分三比二': '比分 3:2', '最终比分为十比九': '最终比分为 10:9',
            '十万': '10 万', '三亿': '三亿', '三点五万': '3.5 万', '三千万': '3000 万',
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(expected, numbers.apply(raw)[0])

    def test_conservative_exclusions(self):
        categories = {
            '日常小数目': ['一个人', '三个问题', '两次', '九个人'],
            '成语固定说法': ['一心一意', '三心二意', '七上八下', '十全十美', '一五一十', '万无一失',
                         '千方百计', '一刀切', '统一', '唯一', '万一', '一样', '一直', '一定', '一些',
                         '一下', '一起', '一般', '百分之百', '十万火急', '一日千里', '三天打鱼', '三天两头'],
            '约数': ['三四个', '十几个', '几十', '上百', '数十', '一两个', '二三十', '七八十',
                   '成千上万', '好几百', '一二枪', '二三十人', '七八十克', '上百人', '数十天',
                   '约十五天', '二十多天', '三百余人', '开火节奏、一二枪命中率'],
            '序数': ['第一', '第十五', '第十五天', '第三千名'],
            '星期节日': ['周一', '星期三', '五一', '十一', '双十一', '双十一当天'],
            '农历': ['八月十五', '正月初五', '农历五月五日', '阴历八月十五日', '腊月二十三'],
            '年份简称': ['八七年', '九八年', '零六年'],
            '专有名称': ['三国', '二郎神', '三体', '五指山', '《一千零一夜》', '《十五岁的天空》'],
            '比分不明': ['三比二', '比例三比二', '三比二更大'],
            '歧义写法': ['一百八毫克', '三千五人', '一万二人', '一亿三千万', '三万五千',
                       '一百百人', '十二月三十二日', '二月三十号', '十三月一日', '3万五千人'],
        }
        for category, cases in categories.items():
            for raw in cases:
                with self.subTest(category=category, raw=raw):
                    # Spacing is independent and still applies to pre-existing Arabic digits.
                    self.assertEqual((raw, []), numbers.apply(raw, cjk_spacing=False))

    def test_spacing_existing_digits_latin_punctuation_and_whitespace(self):
        cases = {
            '616宇宙': '616 宇宙', '手游FPS的上限': '手游 FPS 的上限', '含3.5%的': '含 3.5% 的',
            '比分3:2获胜': '比分 3:2 获胜', '用M4A1开火': '用 M4A1 开火',
            '中A文1字': '中 A 文 1 字', '甲1乙2丙': '甲 1 乙 2 丙',
            '616 宇宙': '616 宇宙', '手游 FPS 的上限': '手游 FPS 的上限', '含  5克': '含  5 克',
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                output, changes = numbers.apply(raw, number_conversion=False)
                self.assertEqual(expected, output)
                self.assertTrue(all(c['layer'] == 'cjk_spacing' for c in changes))
                self.assertEqual((expected, []), numbers.apply(expected, number_conversion=False))

    def test_independent_switches_and_extra_exceptions(self):
        raw = '十五天玩FPS'
        self.assertEqual('15天玩FPS', numbers.apply(raw, cjk_spacing=False)[0])
        self.assertEqual('十五天玩 FPS', numbers.apply(raw, number_conversion=False)[0])
        self.assertEqual((raw, []), numbers.apply(raw, number_conversion=False, cjk_spacing=False))
        self.assertEqual('十五天玩 FPS', numbers.apply(raw, extra_exceptions=['十五天'])[0])
        self.assertEqual('15 天玩 FPS', numbers.apply(raw)[0])  # no mutable global exception list
        for name in ('number_conversion', 'cjk_spacing'):
            for value in (0, 1, 'false', None, [], {}):
                with self.subTest(name=name, value=value), self.assertRaisesRegex(ValueError, '布尔值'):
                    numbers.apply(raw, **{name: value})

    def test_change_records_are_replayable_and_locate_repeated_occurrences(self):
        raw = '十五天、十五天，含百分之三点五，手游FPS，甲1乙2丙。'
        output, changes = numbers.apply(raw)
        self.assertEqual(['十五', '十五', '百分之三点五'],
                         [c['original'] for c in changes if c['layer'] == 'number_conversion'])
        replay = raw
        for layer in ('number_conversion', 'cjk_spacing'):
            records = [c for c in changes if c['layer'] == layer]
            for record in records:
                self.assertEqual(record['original'], replay[record['start']:record['end']])
                self.assertTrue(record['rule'])
            for record in reversed(records):
                replay = replay[:record['start']] + record['replacement'] + replay[record['end']:]
        self.assertEqual(output, replay)
        self.assertEqual((output, []), numbers.apply(output))

    def test_common_sayings_have_no_display_changes(self):
        sayings = [
            '有一天我去了', '一天到晚刷手机', '等我一分钟', '一秒钟都不想等', '三天两夜',
            '一小时后', '一米阳光', '两斤苹果', '一天一夜', '有一年春节', '过两天再说',
            '一天天地等', '一分一秒', '三天两头', '三天打鱼两天晒网', '三心二意', '一心一意',
            '七上八下', '十全十美', '一五一十', '万无一失', '千方百计', '一刀切', '统一',
            '唯一', '万一', '一样', '一直', '一定', '一些', '一下', '一起', '一般', '百分之百',
            '十年如一日', '十年寒窗', '十年树木', '十年磨一剑', '三十年河东', '三十年河西',
            '十万八千里', '十天半个月', '五湖四海', '三番五次', '半斤八两',
        ]
        for raw in sayings:
            with self.subTest(raw=raw):
                self.assertEqual((raw, []), numbers.apply(raw))

    def test_quantity_threshold_includes_ten_and_explicit_decimals(self):
        cases = {
            '九天': '九天', '十天': '10 天', '九个': '九个', '十次': '10 次',
            '十分钟': '10 分钟', '十小时': '10 小时', '十秒': '10 秒', '十秒钟': '10 秒钟',
            '三点五公里': '3.5 公里', '零点五个': '0.5 个', '三点零小时': '3.0 小时',
            '十千克': '10 千克', '三点五千米': '3.5 千米', '十斤苹果': '10 斤苹果', '十两': '10 两',
            '三万粉丝': '三万粉丝', '一万个理由': '一万个理由', '九亿人': '九亿人',
            '二十万': '20 万', '三点五万': '3.5 万', '十亿': '10 亿', '零点五亿': '0.5 亿',
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(expected, numbers.apply(raw)[0])

    def test_ranges_convert_both_ends_or_preserve_both(self):
        cases = {
            '十到二十天': '10 到 20 天', '三十到四十岁': '30 到 40 岁',
            '十至十五分钟': '10 至 15 分钟', '三点五到九点五公里': '3.5 到 9.5 公里',
            '十到十五千克': '10 到 15 千克', '十到二十两': '10 到 20 两', '二十万到三十万人': '20 万到 30 万人',
            '十到二十万': '10 到 20 万', '九到十天': '九到十天', '十到九天': '十到九天',
            '三到二十天': '三到二十天', '十到三天': '十到三天',
            '三点五到九公里': '三点五到九公里', '三万到二十万': '三万到二十万',
            '十至五千克': '十至五千克', '十到一百八毫克': '十到一百八毫克',
            '三四到二十天': '三四到二十天', '约十到二十天': '约十到二十天',
            '第十到二十个': '第十到二十个', '十到三万五千人': '十到三万五千人',
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                output, changes = numbers.apply(raw)
                self.assertEqual(expected, output)
                if expected == raw:
                    self.assertEqual([], changes)
                else:
                    quantities = [c for c in changes if c['layer'] == 'number_conversion']
                    self.assertEqual(1, len(quantities))
                    self.assertEqual('数量范围', quantities[0]['rule'])
                self.assertEqual((expected, []), numbers.apply(expected))
        self.assertEqual(('十到二十天', []), numbers.apply('十到二十天', extra_exceptions=['二十天']))
        raw = '十到二十天，三十至四十岁，三到二十天。'
        output, changes = numbers.apply(raw)
        replay = raw
        for layer in ('number_conversion', 'cjk_spacing'):
            records = [c for c in changes if c['layer'] == layer]
            for record in records:
                self.assertEqual(record['original'], replay[record['start']:record['end']])
            for record in reversed(records):
                replay = replay[:record['start']] + record['replacement'] + replay[record['end']:]
        self.assertEqual(output, replay)

    def test_latin_adjacent_quantities_are_not_models(self):
        cases = {
            '用AI一个人就能做': '用 AI 一个人就能做', '下载QQ两次': '下载 QQ 两次',
            'Claude三天就做完了': 'Claude 三天就做完了', '买了个APP一年会员': '买了个 APP 一年会员',
            'QQ两次': 'QQ 两次', 'AI一个人': 'AI 一个人', 'Claude三天': 'Claude 三天', 'APP一年': 'APP 一年',
            'AI四小龙': 'AI 四小龙', 'GPT四发布': 'GPT 四发布', 'GPT四发布了': 'GPT 四发布了',
            'QQ五千米': 'QQ 五千米', 'AI三万粉丝': 'AI 三万粉丝', 'AI二十万粉丝': 'AI20 万粉丝',
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                output, changes = numbers.apply(raw)
                self.assertEqual(expected, output)
                self.assertFalse(any(c['rule'] == '型号' for c in changes))
        for raw, expected, rule in [('AI十个智能体', 'AI10 个智能体', '统计数量'),
                                    ('QQ二十次', 'QQ20 次', '统计数量'),
                                    ('Claude三点五天', 'Claude3.5 天', '计量'),
                                    ('QQ十五千克', 'QQ15 千克', '计量')]:
            with self.subTest(raw=raw):
                output, changes = numbers.apply(raw)
                self.assertEqual(expected, output)
                self.assertEqual([rule], [c['rule'] for c in changes if c['layer'] == 'number_conversion'])

    def test_model_suffixes_multi_character_numbers_and_explicit_boundaries(self):
        cases = {
            'M四A一': 'M4A1', '五G': '5G', '三D': '3D', '四K': '4K', 'AK四七': 'AK47',
            'iPhone十五': 'iPhone15', 'GPT四。': 'GPT4。', 'GPT四': 'GPT4', 'GPT四 发布': 'GPT4 发布',
            'GPT四，发布': 'GPT4，发布', 'GPT四,发布': 'GPT4,发布', 'GPT四！': 'GPT4！',
            'iPhone十五发布': 'iPhone15 发布', '五G网络': '5G 网络', '三D打印': '3D 打印', '四K画质': '4K 画质',
            '版本M三十': '版本 M30', 'AI二零二六年发布': 'AI2026 年发布',
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(expected, numbers.apply(raw)[0])


if __name__ == '__main__':
    unittest.main()
