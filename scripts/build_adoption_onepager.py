#!/usr/bin/env python3
"""Build the reviewed one-page adoption/risk document in a two-column layout."""
from pathlib import Path

from build_documents import (
    GRAY,
    NAVY,
    TEAL,
    add_inline,
    add_table,
    set_cell_margins,
    set_cell_shading,
    set_run_font,
    setup_document,
)
from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "deliverables/04_词元引擎FDE_采用与风险说明_王泽栋.docx"


def heading(cell, text: str) -> None:
    p = cell.add_paragraph()
    p.paragraph_format.space_before = 0
    p.paragraph_format.space_after = 2
    add_inline(p, f"**{text}**", 10.5, NAVY)


def remove_outer_borders(table) -> None:
    tbl_pr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        node = OxmlElement(f"w:{edge}")
        node.set(qn("w:val"), "nil")
        borders.append(node)
    tbl_pr.append(borders)


def main() -> None:
    doc = setup_document("一页采用与风险说明", landscape=True, compact=True, dense=True)
    title = doc.add_paragraph()
    title.paragraph_format.space_after = 3
    add_inline(title, "**一页采用与风险说明｜会前一页信号，而不是另一套日报**", 15, NAVY)
    note = doc.add_paragraph()
    note.paragraph_format.space_after = 4
    add_inline(note, "依据：M5-01 项目／渠道负责人表12、经营负责人表6、产品负责人表9及角色矩阵表20。耗时为设计目标估计，尚未证明采用效果。", 7.6, GRAY)

    outer = doc.add_table(rows=1, cols=2)
    outer.alignment = WD_TABLE_ALIGNMENT.CENTER
    outer.autofit = False
    remove_outer_borders(outer)
    left, right = outer.rows[0].cells
    for cell in (left, right):
        cell.width = Cm(13.65)
        set_cell_margins(cell, 30, 80, 30, 80)
        cell.paragraphs[0].text = ""

    heading(left, "1. 第一使用者与每次使用")
    p = left.add_paragraph()
    p.paragraph_format.space_after = 3
    add_inline(p, "**项目／渠道负责人**在日／周项目会前、项目启动、人群共创和异常复盘时使用。产品负责人一次确认事实、异常时复核；内容／达人做小测试；经营负责人处理重大决策。不逐条整理原始数据，不长期维护标签。", 7.6)
    add_table(left, [
        ["步骤", "业务动作", "耗时", "即时产出"],
        ["1｜看一页", "查看变化、冲突、未知与优先假设", "1分钟", "带来源、窗口和证据等级的一页信号板"],
        ["2｜翻证据并判断", "看原文件／sheet／行号和反例；点采纳／修改／驳回／待验证", "3分钟", "绑定run/thread/version的判断、责任人和期限"],
        ["3｜会后回填", "选择支持／部分支持／反驳／无结论，必要时补反例", "2分钟", "假设版本更新及下次会议追踪项"],
    ], compact=True)

    heading(left, "4. 数据不可得时的降级")
    add_table(left, [
        ["情况", "可以输出", "明确禁止"],
        ["外部未采集／权限不足", "标未知；授权导出，列补采、来源和权限状态", "不用AI常识或估算冒充事实，不绕权"],
        ["只有人工月报／样本", "只给主题、原声、样本日期和待验证方向", "不算总体问题率，不把7月写成6月"],
        ["M2无同口径全链路", "三组分开，仅比较结构与场次差异", "不拼漏斗，不证长期趋势"],
        ["M4无关联键／观察不足", "分表展示，标不可关联／尚未成熟并延长观察", "不强拼订单，不归因仓库或尺码，不做仓库比较"],
    ], compact=True)

    heading(right, "2. 持续使用机制与人机边界")
    add_table(right, [
        ["机制", "AI负责", "人负责"],
        ["会前自动生成", "沿用授权导出，校验字段／单位／窗口／分母并显示截至日期", "数据负责人确认口径、权限与异常；不让项目负责人临时收表"],
        ["证据翻牌＋反方校验", "展示原文、准确数值、反例、未知和证伪条件", "项目负责人作四态判断；可写口径错误／样本不足／产品不兑现／业务不适用"],
        ["会后结果回流", "待验证项进入下次会议，追踪支持／部分支持／反驳／无结论", "责任人关闭验证；产品、内容和经营负责人决定修改、扩大或停止"],
        ["采用评估", "统计准备时长、四态完成率、到期验证关闭率和证据修正次数", "第4／8／12周决定是否缩减范围；不以聊天轮数或生成字数论成功"],
    ], compact=True)

    heading(right, "3. 三个月失败假设与主要风险")
    p = right.add_paragraph()
    p.paragraph_format.space_after = 3
    add_inline(p, "**最可能失败：** 若仍靠人工反复补表、结果不回写，工具只是多写一份总结，三个月后回到旧表格；YOUNG一手证据持续缺失还会让AI把品牌A旧偏见包装成年轻答案。第4、8、12周检查准备时长、验证关闭率和YOUNG独立证据，无改善就缩小范围。", 7.5)
    add_table(right, [
        ["风险／早期信号", "责任人", "预防／处置"],
        ["采用失败：准备不降、四态积压、重新私发Excel", "项目／渠道负责人", "只留一个会议入口，取消非必要录入，第4周缩到最有用决策"],
        ["样本当总体／错误因果：出现“用户都／问题率为”、M2相除或退款归因", "数据负责人", "强制样本、窗口和证据等级；无分母只定性，禁用跨组漏斗"],
        ["品牌外推／过度承诺：品牌A复制YOUNG、内容先于产品事实", "YOUNG＋产品负责人", "跨品牌默认D级；先确认不可承诺项，小流量可回退验证"],
        ["权限与隐私：来源无权限、出现未脱敏身份或绕过限制", "数据权限负责人", "最小字段、授权采集，记录来源／时间／权限／保留期限"],
    ], compact=True)

    footer = doc.add_paragraph()
    footer.paragraph_format.space_before = 3
    add_inline(footer, "**上线门槛：** 每条结论可回真实来源，YOUNG假设与品牌A事实明确隔离；否则只开放会前证据板，不开放自动动作建议。", 8.0, TEAL)
    doc.save(OUTPUT)
    check = Document(OUTPUT)
    print(f"built {OUTPUT} tables={len(check.tables)}")


if __name__ == "__main__":
    main()
