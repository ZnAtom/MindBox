"""Author the post-inference manual mapping for the isolated DeepSeek pair run."""
import json
from pathlib import Path

from app.client import write_json

ROOT = Path(__file__).resolve().parents[1] / 'var/experiments/model-compare-20261017-deepseek-pairs'

MAPPINGS = {
 'v1': ({'obj_01':'ambiguous','obj_02':'ambiguous','obj_03':'drink','obj_04':'ambiguous'}, ['识别出黄色液体瓶并判定取出，但把抽屉、柜体和吸管列为物品，实例数错误。'], ['obj_01','obj_02','obj_04']),
 'v2': ({'oil-1':'drink','snack-1':'candy','snack-2':'gluten','food-1':'chicken','food-2':'ambiguous'}, ['四个目标物品的轨迹基本识别正确，但额外拆出一个真空包装肉制品，造成实例和净变化错误。'], ['food-2']),
 'v3': ({'obj_001':'ambiguous','obj_002':'ambiguous','obj_003':'chicken','obj_004':'gluten','obj_005':'candy'}, ['三件目标食品均被看见并报告 add，但额外报告收纳箱和盖子；模糊包装按画面轨迹分别对应辣条与糖果。'], ['obj_001','obj_002']),
 'v4': ({'obj-001':'chicken','obj-002':'gluten','obj-003':'candy'}, ['三件目标物品、add 顺序和最终净变化均正确；品牌级别不作为门槛。'], []),
 'v5': ({'obj-001':'candy','obj-002':'gluten','obj-003':'chicken','obj-004':'ambiguous'}, ['三件目标食品被识别并报告 add，但把纸巾包装作为第四件物品。黄色包装按视频目标轨迹对应鸡胸肉。'], ['obj-004']),
 'v6': ({'obj-001':'ambiguous','obj-002':'chicken','obj-003':'ambiguous','obj-004':'candy','obj-005':'gluten','obj-006':'ambiguous'}, ['报告了三件目标之外的多个重复/不明包装，无法建立一一对应；目标轨迹虽部分覆盖，实例数错误。'], ['obj-001','obj-003','obj-006']),
 'v7': ({'obj_001':'chicken','obj_002':'gluten','obj_003':'candy','obj_004':'ambiguous'}, ['识别出三件相关食品和新增糖果，但遗漏先取出面筋、取出鸡胸肉及面筋回放的完整顺序，并把抽屉作为物品。'], ['obj_004']),
 'v8': ({'obj-001':'ambiguous','obj-002':'ambiguous','obj-003':'ambiguous','obj-004':'ambiguous','obj-005':'ambiguous','obj-006':'drink'}, ['识别到瓶装物并报告 add 后又 remove，最终净变化方向错误；额外列出多个收纳容器。'], ['obj-001','obj-002','obj-003','obj-004','obj-005']),
 'v9': ({'obj-001':'drink','obj-002':'ambiguous'}, ['识别到饮料并报告 add，但最终净变化写为 kept，且额外列出抽屉。'], ['obj-002']),
 'v10': ({'obj_001':'drink','obj_002':'chicken','obj_003':'ambiguous','obj_004':'ambiguous','obj_005':'ambiguous'}, ['识别到鸡胸肉 add，但把原有饮料写成 add、漏掉 drink kept，并将抽屉、手和不明包装列为物品。'], ['obj_003','obj_004','obj_005']),
}

reviews = {}
for name, (mapping, notes, errors) in MAPPINGS.items():
    reviews[name] = {'mapping': mapping, 'basic_label_errors': errors, 'notes': notes}
write_json(ROOT / 'review-mappings.json', reviews)
print('wrote', ROOT / 'review-mappings.json')
