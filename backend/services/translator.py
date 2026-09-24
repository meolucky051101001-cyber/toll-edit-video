import logging
import re
import unicodedata

import httpx

from backend.schemas.expansion import QueryPlan

logger = logging.getLogger("research")

_ONLINE_TRANSLATE_CACHE: dict[str, str] = {}

PHRASE_DICTIONARY: dict[str, list[str]] = {
    # Unbox / Đập hộp
    "unbox van phong pham cute": ["可爱文具开箱", "沉浸式文具开箱", "学生文具开箱"],
    "ubox van phong pham cute": ["可爱文具开箱", "沉浸式文具开箱", "学生文具开箱"],
    "dap hop van phong pham cute": ["可爱文具开箱", "沉浸式文具开箱", "学生文具开箱"],
    "van phong pham cute": ["可爱文具", "学生文具", "少女心文具"],
    "unbox van phong pham": ["文具开箱", "可爱文具开箱", "手账好物开箱", "学生文具开箱"],
    "ubox van phong pham": ["文具开箱", "可爱文具开箱", "手账好物开箱", "学生文具开箱"],
    "dap hop van phong pham": ["文具开箱", "可爱文具开箱", "手账好物开箱", "学生文具开箱"],
    "ubox do cute": ["可爱好物开箱", "沉浸式开箱可爱", "盲盒开箱可爱"],
    "unbox do cute": ["可爱好物开箱", "沉浸式开箱可爱", "盲盒开箱可爱"],
    "mo hop do cute": ["可爱好物开箱", "沉浸式开箱可爱", "盲盒开箱可爱"],
    "dap hop do cute": ["可爱好物开箱", "沉浸式开箱可爱", "盲盒开箱可爱"],
    "khui do cute": ["可爱好物开箱", "沉浸式开箱可爱", "盲盒开箱可爱"],
    "do cute": ["可爱好物", "少女心好物", "萌物", "高颜值小物件"],
    "unbox mu": ["盲盒开箱", "拆盲盒", "沉浸式拆盲盒"],
    "unbox blind box": ["盲盒开箱", "拆盲盒", "潮玩盲盒"],
    "unbox sticker": ["咕卡贴纸开箱", "贴纸开箱", "手账贴纸"],
    "dung cu boc sticker": ["咕卡工具", "贴纸镊子", "手账工具"],
    "nhip gap sticker": ["贴纸镊子", "手账镊子", "咕卡镊子"],
    "nhip sticker": ["贴纸镊子", "手账镊子", "咕卡镊子"],
    "nhip": ["镊子"],
    "goi hang cute": ["可爱打包日常", "沉浸式打包", "治愈系打包"],

    "unbox dong ho": ["手表开箱", "腕表开箱", "平价手表开箱"],
    "dap hop dong ho": ["手表开箱", "腕表开箱", "平价手表开箱"],

    # Thú cưng / Nuôi mèo / Nuôi chó
    "nuoi meo": ["养猫", "养猫日常", "新手养猫", "猫咪日常"],
    "cham soc meo": ["养猫", "猫咪护理", "新手养猫"],
    "meo cute": ["可爱猫咪", "萌猫日常", "吸猫"],
    "nuoi cho": ["养狗", "养狗日常", "新手养狗", "狗狗日常"],

    # Sửa chữa xe cộ / Máy móc
    "sua xe may": ["修摩托车", "摩托车维修", "机车维修", "修车"],
    "sua xe": ["修车", "机车维修", "修车日常"],
    "sua xe dap": ["自行车维修", "修自行车"],

    # Máy ảnh / Máy khoan
    "review may anh": ["相机测评", "微单测评", "相机推荐"],
    "unbox may anh": ["相机开箱", "微单开箱", "新相机开箱"],
    "review may khoan": ["电钻测评", "手电钻测评", "家用电钻推荐"],
    "unbox may khoan": ["电钻开箱", "手电钻开箱", "家用电钻开箱"],

    # Âm nhạc / Thu âm
    "thu am": ["录音", "录音日常", "翻唱录音"],

    # Decor / Phòng & Bàn học
    "decor ban hoc vintage": ["复古书桌布置", "复古桌面改造", "复古学生书桌"],
    "ban hoc vintage": ["复古书桌", "复古桌面", "复古学生书桌"],
    "decor ban hoc": ["书桌布置", "桌面改造", "沉浸式书桌布置", "学生书桌"],
    "decor phong ngu": ["卧室改造", "房间布置", "温馨小卧室"],
    "decor ban lam viec": ["办公桌改造", "极简桌面", "桌搭"],
    "decor vintage": ["复古风布置", "复古房间改造", "复古书桌"],

    # Đồ gia dụng / Đời sống
    "do gia dung thong minh": ["智能家居好物", "实用家居好物", "提升幸福感好物"],
    "do gia dung tien ich": ["居家好物", "懒人实用好物", "生活好物"],
    "nha dep": ["温馨小家", "独居日常", "一人居"],

    # Mỹ phẩm / Thời trang / Chăm sóc da
    "son moi": ["口红试色", "热门色号", "显白口红"],
    "review son": ["口红测评", "口红试色", "热门色号"],
    "trang diem": ["美妆教程", "新手化妆", "日常妆容"],
    "duong da": ["护肤分享", "敏肌护肤", "平价护肤"],
    "phoi do mua dong gia re": ["冬季平价穿搭", "冬日平价OOTD", "冬季百元穿搭"],
    "phoi do mua he gia re": ["夏季平价穿搭", "夏日平价OOTD", "夏季百元穿搭"],
    "phoi do mua dong": ["冬季穿搭", "保暖显瘦穿搭", "冬日OOTD", "冬季日常穿搭"],
    "phoi do mua he": ["夏季穿搭", "清爽穿搭", "夏日穿搭"],
    "phoi do": ["日常穿搭", "显瘦穿搭", "OOTD", "穿搭灵感"],
    "ootd": ["OOTD", "今日穿搭", "每日穿搭", "穿搭灵感"],
    "daily ootd": ["每日OOTD", "日常穿搭", "今日穿搭"],
    "vintage": ["复古风", "复古房间改造", "复古穿搭"],
    "y2k": ["Y2K", "Y2K穿搭", "千禧风"],
    "haul": ["好物分享", "好物开箱", "沉浸式开箱"],
    "son mong tay": ["显白美甲", "可爱美甲款式", "美甲教程"],
    "nail cute": ["可爱美甲款式", "少女心美甲", "显白美甲"],

    # Trị mụn / Skincare chuyên sâu
    "meo tri mun an cho da dau": ["油皮闭口去除技巧", "油痘肌护肤", "去闭口技巧"],
    "tri mun an cho da dau": ["油皮去闭口", "油皮去粉刺", "去闭口护肤"],
    "meo tri mun an": ["去闭口技巧", "祛闭口粉刺", "去闭口护肤"],
    "tri mun an": ["去闭口技巧", "祛闭口粉刺", "去闭口护肤"],
    "meo tri mun": ["祛痘小技巧", "快速祛痘", "祛痘护肤分享"],
    "tri mun": ["祛痘护肤", "祛痘印", "敏肌祛痘"],
    "kem chong nang": ["防晒霜推荐", "平价防晒测评", "控油防晒"],
    "sua rua mat cho da dau": ["油皮洗面奶推荐", "控油洁面测评", "油痘肌洗面奶"],
    "sua rua mat": ["洗面奶测评", "平价洗面奶推荐", "温和洁面"],

    # Mỹ phẩm & Skincare bổ sung
    "ke mat": ["眼线教程", "新手画眼线", "眼线技巧"],
    "skincare": ["护肤流程", "日常护肤", "沉浸式护肤"],
    "phan phu": ["定妆散粉", "控油散粉", "定妆粉饼"],
    "tay trang": ["温和卸妆", "卸妆油推荐", "卸妆清洁"],
    "duong am": ["修护面霜", "保湿补水", "滋润面霜"],
    "cham soc toc": ["护发分享", "柔顺秀发", "养发护发"],
    "mut tan kem nen": ["美妆蛋推荐", "气垫粉扑", "底妆粉扑"],
    "mut tan": ["美妆蛋", "粉扑", "底妆工具"],

    # Thời trang bổ sung
    "outfit di hoc": ["学生日常OOTD", "大学穿搭", "减龄穿搭"],
    "outfit mua thu": ["秋季穿搭", "秋日OOTD", "温柔风穿搭"],
    "outfit": ["日常穿搭", "OOTD", "穿搭分享"],
    "ao len co lo": ["高领毛衣穿搭", "宽松毛衣搭配", "秋冬毛衣"],
    "ao len": ["毛衣穿搭", "秋冬毛衣", "温柔风毛衣"],
    "ao blazer": ["西装外套穿搭", "韩系西装搭配", "通勤西装"],
    "blazer": ["西装外套穿搭", "廓形西装", "韩系西装"],
    "vay hoa nhi": ["法式碎花裙", "复古碎花连衣裙", "夏日碎花裙"],
    "vay hoa": ["碎花裙穿搭", "夏日连衣裙", "法式复古裙"],
    "set do the thao": ["运动套装穿搭", "休闲运动风", "冬季运动套装"],
    "do the thao": ["运动风穿搭", "运动套装", "休闲穿搭"],
    "nguoi lun": ["小个子穿搭", "显高穿搭", "小个子显高技巧"],

    # Thủ công bổ sung
    "lam gom": ["手工陶艺", "陶艺体验", "拉胚日常"],
    "gom": ["陶艺制作", "手工陶瓷", "手作陶瓷"],
    "gap hoa giay": ["折纸花教程", "手工折纸", "立体纸花"],
    "hoa giay": ["折纸花", "手工纸花", "折纸教程"],
    "moc len thu bong": ["毛线玩偶钩织", "新手钩织玩偶", "钩针玩偶教程"],
    "thu bong": ["毛线玩偶", "钩织玩偶", "手作玩偶"],
    "bang keo washi": ["手账和纸胶带", "手账胶带", "咕卡胶带"],
    "washi tape": ["和纸胶带", "手账胶带", "胶带分装"],

    # Ẩm thực bổ sung
    "pho bo": ["牛肉粉做法", "自制牛肉米粉", "家庭版牛肉粉"],
    "pho": ["自制米粉", "牛肉粉教程", "家庭版米粉"],
    "mang ot": ["自制泡椒竹笋", "腌制酸辣竹笋", "酸辣竹笋做法"],

    # Công nghệ & Đời sống bổ sung
    "chuot khong day": ["无线静音鼠标", "蓝牙无线鼠标", "办公无线鼠标"],
    "tai nghe bluetooth": ["无线蓝牙耳机", "主动降噪耳机", "平价蓝牙耳机"],
    "day sac nhanh": ["快充数据线", "多功能快充线", "Type-C快充线"],
    "may uon toc": ["自动卷发棒", "蛋卷棒教程", "新手卷发棒"],
    "may say toc": ["高速吹风机", "负离子吹风机", "便携吹风机"],
    "hop dung my pham": ["化妆品收纳盒", "桌面亚克力收纳", "护肤品收纳置物架"],
    "binh giu nhiet": ["大容量保温杯", "高颜值保温水杯", "不锈钢保温杯"],

    # Công nghệ / Bàn phím cơ
    "review ban phim co": ["机械键盘测评", "机械键盘推荐", "平价机械键盘"],
    "ban phim co gia re": ["百元机械键盘", "平价机械键盘推荐", "性价比机械键盘"],
    "ban phim co": ["机械键盘推荐", "机械键盘测评", "客制化键盘"],
    "ban phim": ["机械键盘推荐", "机械键盘测评", "键盘分享"],

    # Nấu ăn / Bánh trái / Đồ uống
    "nau an": ["做饭日常", "一人食", "家常菜", "快手菜"],
    "mon ngon": ["美食教程", "减脂餐", "深夜食堂"],
    "lam banh": ["烘焙教程", "新手做蛋糕", "甜品教程"],
    "cach lam banh bao": ["包子制作教程", "手工包子", "做包子"],
    "lam banh bao": ["包子制作教程", "手工包子", "做包子"],
    "banh bao": ["包子制作教程", "手工包子", "包子做法"],
    "cach lam banh mi": ["自制面包教程", "做面包", "家庭烘焙面包"],
    "lam banh mi": ["自制面包教程", "做面包", "家庭烘焙面包"],
    "banh mi": ["自制面包教程", "做面包", "法棍教程"],
    "cach lam banh cuon": ["自制肠粉教程", "肠粉做法", "家常肠粉"],
    "lam banh cuon": ["自制肠粉教程", "肠粉做法", "家常肠粉"],
    "banh cuon": ["自制肠粉教程", "肠粉做法", "家常肠粉"],
    "cach lam banh xeo": ["越南煎饼教程", "自制脆皮煎饼", "家常煎饼"],
    "lam banh xeo": ["越南煎饼教程", "自制脆皮煎饼", "家常煎饼"],
    "banh xeo": ["越南煎饼教程", "自制脆皮煎饼", "家常煎饼"],
    "cach lam sua chua": ["自制酸奶教程", "家庭做酸奶", "自制酸奶做法"],
    "lam sua chua": ["自制酸奶教程", "家庭做酸奶", "自制酸奶做法"],
    "sua chua": ["自制酸奶", "酸奶教程", "酸奶好物"],
    "cach lam sua hat": ["自制豆浆教程", "自制植物奶", "破壁机养生豆浆"],
    "lam sua hat": ["自制豆浆教程", "自制植物奶", "破壁机养生豆浆"],
    "sua hat": ["自制豆浆", "养生破壁机豆浆", "植物奶教程"],
    "cach pha ca phe muoi": ["海盐咖啡制作教程", "自制特调咖啡", "特调海盐咖啡"],
    "pha ca phe muoi": ["海盐咖啡制作教程", "自制特调咖啡", "特调海盐咖啡"],
    "ca phe muoi": ["海盐咖啡", "自制海盐咖啡", "特调咖啡教程"],
    "cach lam tra sua": ["自制奶茶教程", "家常手作奶茶", "网红奶茶做法"],
    "tra sua": ["自制奶茶", "手作奶茶", "奶茶教程"],

    # Gym / Thể hình
    "bai tap gym tang co lung": ["背肌训练动作", "健身房练背教学", "背部肌肉塑形"],
    "bai tap tang co lung": ["背肌训练动作", "健身房练背教学", "背部肌肉塑形"],
    "tang co lung": ["背肌训练动作", "健身房练背教学", "背部肌肉塑形"],
    "bai tap co lung": ["背部训练动作", "练背教学", "背肌塑形"],
    "bai tap gym cho nguoi moi": ["新手健身房指南", "新手力量训练", "健身入门教程"],
    "tap gym cho nguoi moi": ["新手健身房指南", "新手力量训练", "健身入门教程"],
    "tap gym": ["健身房训练", "力量训练", "健身日常"],
    "bai tap bung": ["马甲线核心训练", "减肚子动作", "腹肌训练教学"],
    "giam mo bung": ["减肚子核心训练", "瘦腹部动作", "减脂核心训练"],

    # Thủ công / Móc len
    "huong dan moc len hoa tulip": ["钩织郁金香教程", "手工毛线花", "毛线郁金香编织"],
    "moc len hoa tulip": ["钩织郁金香", "手工钩织花朵", "毛线花教程"],
    "huong dan moc len": ["钩针新手教程", "毛线钩织入门", "手工编织教程"],
    "moc len": ["手工钩织", "毛线编织", "钩针编织"],

    # Vlogs & Đời sống
    "vlog hang ngay": ["沉浸式日常", "沉浸式vlog", "周末日常"],
    "vlog di hoc": ["大学日常", "高中日常", "学习vlog"],
    "vlog di lam": ["打工人日常", "上班族日常", "通勤日常"],
}

MANDATORY_MAP: dict[str, list[str]] = {
    "mua dong": ["冬季", "冬日", "冬"],
    "mua he": ["夏季", "夏日", "夏"],
    "mua thu": ["秋季", "秋日", "秋"],
    "mua xuan": ["春季", "春日", "春"],
    "vintage": ["复古"],
    "co dien": ["复古"],
    "y2k": ["Y2K"],
    "hoc sinh": ["学生"],
    "sinh vien": ["学生", "大学"],
    "di lam": ["通勤", "上班族"],
    "di hoc": ["上学", "学生"],
    "gia re": ["平价", "性价比", "百元"],
}

SUBJECT_MAP: dict[str, dict[str, list[str]]] = {
    "nuoi meo": {"primary": ["养猫"], "natural": ["猫咪日常"], "narrow": ["新手养猫"]},
    "meo": {"primary": ["猫咪"], "natural": ["小猫"], "narrow": ["吸猫"]},
    "nuoi cho": {"primary": ["养狗"], "natural": ["狗狗日常"], "narrow": ["新手养狗"]},
    "cho": {"primary": ["狗狗"], "natural": ["小狗"], "narrow": ["修狗"]},
    "thu cung": {"primary": ["宠物"], "natural": ["萌宠"], "narrow": ["宠物日常"]},
    "sua xe may": {"primary": ["修摩托车"], "natural": ["机车维修"], "narrow": ["修车"]},
    "sua xe": {"primary": ["修车"], "natural": ["机车维修"], "narrow": ["修车日常"]},
    "xe may": {"primary": ["摩托车"], "natural": ["机车"], "narrow": ["踏板车"]},
    "xe dap": {"primary": ["自行车"], "natural": ["单车"], "narrow": ["公路车"]},
    "dong ho": {"primary": ["手表"], "natural": ["腕表"], "narrow": ["平价手表"]},
    "may anh": {"primary": ["相机"], "natural": ["微单相机"], "narrow": ["照相机"]},
    "may quay": {"primary": ["摄像机"], "natural": ["运动相机", "相机"], "narrow": ["vlog相机"]},
    "may khoan": {"primary": ["电钻"], "natural": ["手电钻"], "narrow": ["家用电钻"]},
    "thu am": {"primary": ["录音"], "natural": ["翻唱录音"], "narrow": ["录音日常"]},
    "van phong pham": {"primary": ["文具"], "natural": ["手账文具"], "narrow": ["学生文具"]},
    "nhip gap sticker": {"primary": ["贴纸镊子"], "natural": ["手账贴纸镊子"], "narrow": ["咕卡镊子"]},
    "nhip sticker": {"primary": ["贴纸镊子"], "natural": ["手账贴纸镊子"], "narrow": ["咕卡镊子"]},
    "nhip": {"primary": ["镊子"], "natural": ["手账镊子"], "narrow": ["贴纸镊子"]},
    "sticker": {"primary": ["贴纸"], "natural": ["手账贴纸"], "narrow": ["咕卡贴纸"]},
    "so": {"primary": ["手账"], "natural": ["手账本"], "narrow": ["手账好物"]},
    "ban hoc": {"primary": ["书桌"], "natural": ["桌面", "桌面改造", "桌搭"], "narrow": ["学生书桌"]},
    "ban lam viec": {"primary": ["办公桌"], "natural": ["极简桌面"], "narrow": ["桌搭"]},
    "phong ngu": {"primary": ["卧室"], "natural": ["房间布置"], "narrow": ["温馨卧室"]},
    "son moi": {"primary": ["口红"], "natural": ["热门色号"], "narrow": ["显白口红"]},
    "son": {"primary": ["口红"], "natural": ["热门色号"], "narrow": ["显白口红"]},
    "my pham": {"primary": ["美妆", "化妆品"], "natural": ["彩妆", "护肤品"], "narrow": ["平价美妆"]},
    "trang diem": {"primary": ["化妆"], "natural": ["日常妆容"], "narrow": ["新手化妆"]},
    "duong da": {"primary": ["护肤"], "natural": ["护肤分享"], "narrow": ["敏肌护肤"]},
    "phoi do": {"primary": ["穿搭"], "natural": ["日常穿搭"], "narrow": ["OOTD"]},
    "quan ao": {"primary": ["衣服"], "natural": ["穿搭"], "narrow": ["日常穿搭"]},
    "gia dung": {"primary": ["家居"], "natural": ["家居好物"], "narrow": ["生活好物"]},
    "do gia dung": {"primary": ["家居好物"], "natural": ["居家好物"], "narrow": ["实用好物"]},
    "hop mu": {"primary": ["盲盒"], "natural": ["潮玩盲盒"], "narrow": ["拆盲盒"]},
    "blindbox": {"primary": ["盲盒"], "natural": ["潮玩盲盒"], "narrow": ["拆盲盒"]},
    "blind box": {"primary": ["盲盒"], "natural": ["潮玩盲盒"], "narrow": ["拆盲盒"]},
    "do cute": {"primary": ["可爱好物", "可爱"], "natural": ["萌物", "少女心", "萌"], "narrow": ["少女心好物", "高颜值小物件"]},
    "nau an": {"primary": ["做饭"], "natural": ["一人食"], "narrow": ["家常菜"]},
    "mon ngon": {"primary": ["美食"], "natural": ["深夜食堂"], "narrow": ["减脂餐"]},
    "lam banh bao": {"primary": ["包子"], "natural": ["做包子"], "narrow": ["包子教程"]},
    "banh bao": {"primary": ["包子"], "natural": ["做包子"], "narrow": ["包子教程"]},
    "banh mi": {"primary": ["面包"], "natural": ["自制面包"], "narrow": ["法棍"]},
    "banh cuon": {"primary": ["肠粉"], "natural": ["自制肠粉"], "narrow": ["肠粉做法"]},
    "banh xeo": {"primary": ["越南煎饼"], "natural": ["脆皮煎饼", "煎饼"], "narrow": ["煎饼做法"]},
    "lam banh": {"primary": ["烘焙"], "natural": ["甜品"], "narrow": ["做蛋糕"]},
    "sua chua": {"primary": ["酸奶"], "natural": ["自制酸奶"], "narrow": ["酸奶教程"]},
    "sua hat": {"primary": ["豆浆"], "natural": ["自制豆浆"], "narrow": ["植物奶"]},
    "sua rua mat": {"primary": ["洗面奶"], "natural": ["控油洁面"], "narrow": ["温和洁面"]},
    "ban phim co": {"primary": ["机械键盘"], "natural": ["机械键盘测评"], "narrow": ["客制化键盘"]},
    "ban phim": {"primary": ["键盘"], "natural": ["机械键盘"], "narrow": ["桌面好物"]},
    "tri mun an": {"primary": ["去闭口"], "natural": ["油皮闭口"], "narrow": ["去闭口技巧"]},
    "tri mun": {"primary": ["祛痘"], "natural": ["祛痘印"], "narrow": ["祛痘护肤"]},
    "ca phe muoi": {"primary": ["海盐咖啡"], "natural": ["自制海盐咖啡"], "narrow": ["特调咖啡"]},
    "ca phe": {"primary": ["咖啡"], "natural": ["自制咖啡"], "narrow": ["特调咖啡"]},
    "co lung": {"primary": ["背肌"], "natural": ["练背动作"], "narrow": ["背部训练"]},
    "tap gym": {"primary": ["健身"], "natural": ["力量训练"], "narrow": ["健身日常"]},
    "moc len": {"primary": ["钩织"], "natural": ["手工钩织"], "narrow": ["毛线编织"]},
    "son mong tay": {"primary": ["美甲"], "natural": ["显白美甲"], "narrow": ["美甲款式"]},
    "nail": {"primary": ["美甲"], "natural": ["显白美甲"], "narrow": ["美甲款式"]},
    "vlog": {"primary": ["日常"], "natural": ["沉浸式日常"], "narrow": ["vlog"]},
    "ke mat": {"primary": ["眼线"], "natural": ["眼线教程"], "narrow": ["新手画眼线"]},
    "skincare": {"primary": ["护肤"], "natural": ["日常护肤"], "narrow": ["护肤流程"]},
    "phan phu": {"primary": ["散粉"], "natural": ["控油散粉"], "narrow": ["定妆粉饼"]},
    "tay trang": {"primary": ["卸妆"], "natural": ["温和卸妆"], "narrow": ["卸妆油"]},
    "duong am": {"primary": ["保湿"], "natural": ["修护面霜"], "narrow": ["面霜"]},
    "cham soc toc": {"primary": ["护发"], "natural": ["养发"], "narrow": ["柔顺秀发"]},
    "ao len": {"primary": ["毛衣"], "natural": ["秋冬毛衣"], "narrow": ["温柔风毛衣"]},
    "blazer": {"primary": ["西装"], "natural": ["西装外套"], "narrow": ["通勤西装"]},
    "vay hoa": {"primary": ["碎花裙"], "natural": ["复古碎花裙", "碎花"], "narrow": ["法式连衣裙"]},
    "do the thao": {"primary": ["运动套装"], "natural": ["运动风", "运动"], "narrow": ["休闲运动"]},
    "lam gom": {"primary": ["陶艺"], "natural": ["手工陶艺"], "narrow": ["拉胚"]},
    "hoa giay": {"primary": ["折纸花"], "natural": ["手工纸花", "折纸"], "narrow": ["折纸教程"]},
    "pho bo": {"primary": ["牛肉粉"], "natural": ["自制米粉"], "narrow": ["牛肉粉教程"]},
    "mang ot": {"primary": ["泡椒竹笋"], "natural": ["酸辣竹笋", "竹笋"], "narrow": ["腌制竹笋"]},
}

ACTION_MAP: dict[str, list[str]] = {
    "unbox": ["开箱", "拆箱", "拆袋", "拆盲盒", "拆"],
    "ubox": ["开箱", "拆箱", "拆袋", "拆盲盒", "拆"],
    "khui": ["开箱", "拆箱", "拆袋", "拆盲盒", "拆"],
    "dap hop": ["开箱", "拆箱", "拆袋", "拆盲盒", "拆"],
    "mo hop": ["开箱", "拆箱", "拆袋", "拆盲盒", "拆"],
    "decor": ["布置", "改造"],
    "trang tri": ["布置", "改造", "手账", "装饰"],
    "phoi do": ["穿搭", "搭配"],
    "review": ["测评", "试色"],
    "danh gia": ["测评"],
    "goi hang": ["打包", "沉浸式打包"],
    "nuoi": ["养", "饲养"],
    "sua": ["维修", "修"],
}

MODIFIER_MAP: dict[str, list[str]] = {
    "cute": ["可爱", "萌物", "少女心"],
    "de thuong": ["可爱", "萌物", "少女心"],
    "dang yeu": ["可爱", "萌物"],
    "thong minh": ["智能"],
    "tien ich": ["好物", "实用"],
    "xinh": ["高颜值", "好看"],
    "dep": ["好看", "高颜值"],
    "nho": ["迷你"],
}

WORD_MAP: dict[str, list[str]] = {
    "ubox": ["开箱"],
    "unbox": ["开箱"],
    "khui": ["开箱"],
    "dap hop": ["开箱"],
    "mo hop": ["开箱"],
    "review": ["测评"],
    "danh gia": ["测评"],
    "cute": ["可爱", "萌物", "少女心"],
    "de thuong": ["可爱", "萌物"],
    "dang yeu": ["可爱", "萌物"],
    "xinh": ["高颜值", "好看"],
    "dep": ["好看", "高颜值"],
    "ban hoc": ["书桌"],
    "decor": ["布置", "改造"],
    "trang tri": ["布置", "装饰"],
    "van phong pham": ["文具"],
    "so": ["手账"],
    "but": ["笔"],
    "nhip": ["镊子"],
    "sticker": ["贴纸", "咕卡"],
    "hop mu": ["盲盒"],
    "blindbox": ["盲盒"],
    "gia dung": ["家居"],
    "thong minh": ["智能"],
    "tien ich": ["好物", "实用"],
    "quan ao": ["穿搭", "衣服"],
    "outfit": ["穿搭"],
    "vay": ["连衣裙"],
    "son": ["口红"],
    "my pham": ["美妆"],
    "makeup": ["化妆"],
    "nau an": ["做饭", "美食"],
    "an vat": ["零食"],
    "meo": ["猫咪", "小猫", "养猫"],
    "cho": ["狗狗", "修狗", "养狗"],
    "xe may": ["摩托车", "机车"],
    "sua xe": ["修车", "机车维修"],
    "dong ho": ["手表", "腕表"],
    "may anh": ["相机", "照相机"],
    "may khoan": ["电钻", "手电钻"],
    "thu am": ["录音"],
}


def normalize_vietnamese(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.lower()).replace("đ", "d")
    normalized = "".join(c for c in normalized if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", normalized).strip()


def is_cjk(text: str) -> bool:
    if not text:
        return False
    clean = text.replace(" ", "").replace("#", "")
    if not clean:
        return False
    cjk_count = sum(1 for c in clean if "\u4e00" <= c <= "\u9fff")
    return cjk_count >= max(1, len(clean) * 0.4)


HASHTAG_SEGMENTATION_MAP: dict[str, str] = {
    "banphimco": "bàn phím cơ",
    "banphim": "bàn phím",
    "decorbanhoc": "decor bàn học",
    "decorphongngu": "decor phòng ngủ",
    "decorbanlamviec": "decor bàn làm việc",
    "decorvintage": "decor vintage",
    "vanphongpham": "văn phòng phẩm",
    "dungcubocsticker": "dụng cụ bóc sticker",
    "bocsticker": "bóc sticker",
    "giamcan": "giảm cân",
    "tangco": "tăng cơ",
    "tangcolung": "tăng cơ lưng",
    "tapgym": "tập gym",
    "meotrimunan": "mẹo trị mụn ẩn",
    "trimunan": "trị mụn ẩn",
    "trimun": "trị mụn",
    "tritham": "trị thâm",
    "kemchongnang": "kem chống nắng",
    "suaruamat": "sữa rửa mặt",
    "duongda": "dưỡng da",
    "duongam": "dưỡng ẩm",
    "skincare": "skincare",
    "sonmoi": "son môi",
    "reviewson": "review son",
    "trangdiem": "trang điểm",
    "phoido": "phối đồ",
    "phoidomuadong": "phối đồ mùa đông",
    "phoidomuahe": "phối đồ mùa hè",
    "sonmongtay": "sơn móng tay",
    "lamdep": "làm đẹp",
    "nauan": "nấu ăn",
    "anvat": "ăn vặt",
    "meocute": "mèo cute",
    "nuoimeo": "nuôi mèo",
    "nuoicho": "nuôi chó",
    "suaxe": "sửa xe",
    "suaxemay": "sửa xe máy",
    "dongho": "đồng hồ",
    "mayanh": "máy ảnh",
    "maykhoan": "máy khoan",
    "thuam": "thu âm",
    "caphemuoi": "cà phê muối",
    "trasua": "trà sữa",
    "banhbao": "bánh bao",
    "banhmi": "bánh mì",
    "docute": "đồ cute",
    "unbox": "unbox",
    "daphop": "đập hộp",
    "ootd": "ootd",
    "haul": "haul",
    "vintage": "vintage",
    "y2k": "y2k",
    "blindbox": "blind box",
    "hopmu": "hộp mù",
    "goclamviec": "góc làm việc",
    "gocbanhoc": "góc bàn học",
    "chamsocmeo": "chăm sóc mèo",
    "suaxedap": "sửa xe đạp",
}


def extract_hashtag_intent(query: str) -> tuple[str, list[str]]:
    """
    Extracts hashtags from query, segments unspaced words, and returns:
    (clean_query_text, list_of_raw_hashtags)
    Examples:
    - "#decorbanhoc" -> ("decor bàn học", ["decorbanhoc"])
    - "phối đồ #ootd" -> ("phối đồ ootd", ["ootd"])
    - "#banphimco giá rẻ" -> ("bàn phím cơ giá rẻ", ["banphimco"])
    """
    raw_tags = re.findall(r"#([^\s#]+)", query)
    cleaned_query = query

    for tag in raw_tags:
        norm_tag = normalize_vietnamese(tag).replace("_", "").replace("-", "")
        segmented = HASHTAG_SEGMENTATION_MAP.get(norm_tag)
        if segmented:
            cleaned_query = cleaned_query.replace(f"#{tag}", f" {segmented} ")
        else:
            parts = [p for p in re.split(r"[_\-]+", tag) if p]
            if len(parts) > 1:
                cleaned_query = cleaned_query.replace(f"#{tag}", f" {' '.join(parts)} ")
            else:
                cleaned_query = cleaned_query.replace(f"#{tag}", f" {tag} ")

    cleaned_query = re.sub(r"^#+\s*", "", cleaned_query)
    cleaned_query = re.sub(r"\s+", " ", cleaned_query).strip()

    norm_full = normalize_vietnamese(query).lstrip("#").replace("_", "").replace("-", "")
    if norm_full in HASHTAG_SEGMENTATION_MAP:
        cleaned_query = HASHTAG_SEGMENTATION_MAP[norm_full]

    return cleaned_query, raw_tags


RESTORATION_MAP: dict[str, str] = {
    "ban phim co gia re": "bàn phím cơ giá rẻ",
    "review ban phim co": "review bàn phím cơ",
    "ban phim co": "bàn phím cơ",
    "ban phim": "bàn phím",
    "chuot khong day": "chuột không dây",
    "tai nghe bluetooth": "tai nghe bluetooth",
    "meo tri mun an cho da dau": "mẹo trị mụn ẩn cho da dầu",
    "tri mun an cho da dau": "trị mụn ẩn cho da dầu",
    "meo tri mun an": "mẹo trị mụn ẩn",
    "tri mun an": "trị mụn ẩn",
    "meo tri mun": "mẹo trị mụn",
    "tri mun": "trị mụn",
    "tri tham": "trị thâm",
    "kem chong nang": "kem chống nắng",
    "sua rua mat cho da dau": "sữa rửa mặt cho da dầu",
    "sua rua mat": "sữa rửa mặt",
    "cach pha ca phe muoi": "cách pha cà phê muối",
    "pha ca phe muoi": "pha cà phê muối",
    "ca phe muoi": "cà phê muối",
    "cach lam tra sua": "cách làm trà sữa",
    "tra sua": "trà sữa",
    "cach lam banh bao": "cách làm bánh bao",
    "lam banh bao": "làm bánh bao",
    "banh bao": "bánh bao",
    "cach lam banh mi": "cách làm bánh mì",
    "lam banh mi": "làm bánh mì",
    "banh mi": "bánh mì",
    "cach lam banh cuon": "cách làm bánh cuốn",
    "lam banh cuon": "làm bánh cuốn",
    "banh cuon": "bánh cuốn",
    "cach lam banh xeo": "cách làm bánh xèo",
    "lam banh xeo": "làm bánh xèo",
    "banh xeo": "bánh xèo",
    "cach lam sua chua": "cách làm sữa chua",
    "lam sua chua": "làm sữa chua",
    "sua chua": "sữa chua",
    "cach lam sua hat": "cách làm sữa hạt",
    "lam sua hat": "làm sữa hạt",
    "sua hat": "sữa hạt",
    "bai tap gym tang co lung": "bài tập gym tăng cơ lưng",
    "bai tap tang co lung": "bài tập tăng cơ lưng",
    "tang co lung": "tăng cơ lưng",
    "bai tap co lung": "bài tập cơ lưng",
    "bai tap gym cho nguoi moi": "bài tập gym cho người mới",
    "tap gym cho nguoi moi": "tập gym cho người mới",
    "bai tap gym": "bài tập gym",
    "tap gym": "tập gym",
    "bai tap bung": "bài tập bụng",
    "giam mo bung": "giảm mỡ bụng",
    "giam can": "giảm cân",
    "giam mo": "giảm mỡ",
    "tang co": "tăng cơ",
    "huong dan moc len hoa tulip": "hướng dẫn móc len hoa tulip",
    "moc len hoa tulip": "móc len hoa tulip",
    "huong dan moc len": "hướng dẫn móc len",
    "moc len": "móc len",
    "son mong tay": "sơn móng tay",
    "meo vat": "mẹo vặt",
    "meo hay": "mẹo hay",
}


def restore_vietnamese_phrases(text: str) -> str:
    """Khôi phục dấu tiếng Việt cho các cụm từ phổ biến khi người dùng gõ không dấu."""
    lower = text.lower()
    restored = text
    for phrase, replacement in sorted(RESTORATION_MAP.items(), key=lambda x: len(x[0]), reverse=True):
        pattern = r"\b" + re.escape(phrase) + r"\b"
        if re.search(pattern, lower):
            restored = re.sub(pattern, replacement, restored, flags=re.IGNORECASE)
            lower = restored.lower()
    return restored


def is_valid_subject_match(raw: str, norm: str, key: str) -> bool:
    """Kiểm tra ngữ cảnh thực tế của chủ đề để tránh nhầm lẫn từ đồng âm/mất dấu tiếng Việt."""
    if key == "cho":
        if re.search(r"\bchó\b", raw, flags=re.IGNORECASE):
            return True
        dog_qualifiers = (
            r"\b(?:nuoi|con|chu|thuc an|tam|huan luyen|phu kien|do cho|giong)\s+cho\b"
            r"|\bcho\s+(?:con|cung|canh|poodle|corgi|pug|husky|nho|cute|dom|co)\b"
        )
        return bool(re.search(dog_qualifiers, norm))
    if key == "meo":
        if re.search(r"\bmẹo\b", raw, flags=re.IGNORECASE):
            return False
        if re.search(r"\bmèo\b", raw, flags=re.IGNORECASE):
            return True
        tip_indicators = r"\bmeo\s+(?:tri|mun|vat|hay|lam|nau|chup|phoi|hoc|lam dep|cho)\b"
        if re.search(tip_indicators, norm):
            return False
        cat_qualifiers = (
            r"\b(?:nuoi|con|chu|thuc an|tam|cat|hinh|phu kien|do cho)\s+meo\b"
            r"|\bmeo\s+(?:con|cung|canh|cute|anh|muop|tai cup|bengal|ba tu|nho|meo)\b"
        )
        if re.search(cat_qualifiers, norm):
            return True
        return not bool(re.search(r"\b(?:tri|mun|vat|hay|lam dep|cho da)\b", norm))
    if key == "so":
        if re.search(r"\bsổ\b", raw, flags=re.IGNORECASE):
            return True
        return bool(
            re.search(
                r"\b(?:so\s+(?:tay|cong|ghi\s+chep|planner|bullet|da)|cuon\s+so|tap\s+so)\b",
                norm,
            )
        )
    if key == "son":
        return not bool(re.search(r"\bson\s+(?:mong|nha|tuong|xe|mai|gel)\b", norm))
    if key == "lam banh":
        return not bool(
            re.search(r"\blam\s+banh\s+(?:bao|cuon|mi|xeo|trang|chung|gio|beo|pia|loc|tet)\b", norm)
        )
    if key == "sua":
        if re.search(r"\bsữa\b", raw, flags=re.IGNORECASE):
            return False
        if re.search(
            r"\bsua\s+(?:chua|rua\s+mat|hat|tam|tuoi|dac|dau\s+nanh|bap|me|tam|duong)\b",
            norm,
        ):
            return False
    return True


def is_valid_action_match(raw: str, norm: str, key: str) -> bool:
    """Kiểm tra hành động để tránh nhầm lẫn 'sua' (sửa chữa vs sữa thực phẩm/mỹ phẩm)."""
    if key in ("sua", "sua chua"):
        if re.search(r"\bsữa\b", raw, flags=re.IGNORECASE):
            return False
        if re.search(
            r"\bsua\s+(?:chua|rua\s+mat|hat|tam|tuoi|dac|dau\s+nanh|bap|me|tam|duong)\b",
            norm,
        ):
            return False
    return True


def is_valid_phrase_match(raw: str, norm: str, phrase: str) -> bool:
    """Bảo vệ cụm từ điển không bị over-match sai ngữ cảnh."""
    if phrase == "lam banh":
        if re.search(r"\blam\s+banh\s+(?:bao|cuon|mi|xeo|trang|chung|gio|beo|pia|loc|tet)\b", norm):
            return False
    elif phrase == "meo cute":
        if re.search(r"\bmẹo\b", raw, flags=re.IGNORECASE):
            return False
    elif phrase == "pho":
        if re.search(r"\bphố\b", raw, flags=re.IGNORECASE) or not re.search(
            r"\b(?:pho|phở)\s+(?:bo|ga|ha\s+noi|tai|nam|sot\s+vang|xao|cuon|chua)\b|\b(?:an|nau|bat|to|quan|lam)\s+(?:pho|phở)\b",
            norm,
        ):
            return False
    return True


def extract_subject(raw_query: str, norm: str | None = None) -> str:
    if norm is None:
        norm = normalize_vietnamese(raw_query)
    for key in sorted(SUBJECT_MAP.keys(), key=len, reverse=True):
        if re.search(r"\b" + re.escape(key) + r"\b", norm):
            if is_valid_subject_match(raw_query, norm, key):
                return key
    return ""


def extract_action(raw_query: str, norm: str | None = None) -> str | None:
    if norm is None:
        norm = normalize_vietnamese(raw_query)
    for key in sorted(ACTION_MAP.keys(), key=len, reverse=True):
        if re.search(r"\b" + re.escape(key) + r"\b", norm):
            if is_valid_action_match(raw_query, norm, key):
                return key
    return None


def synthesize_tiered_from_translation(chinese_text: str, original_query: str) -> list[str]:
    """Tạo 2-3 truy vấn phân tầng từ kết quả dịch trực tuyến tiếng Trung."""
    clean = re.sub(r"\s+", "", chinese_text)
    if not clean:
        return [original_query]
    results = [clean]

    norm = normalize_vietnamese(original_query)
    if any(k in clean for k in ("测评", "评测")) or any(k in norm for k in ("review", "danh gia")):
        core = clean.replace("测评", "").replace("评测", "")
        if core:
            alt2 = f"{core}推荐"
            alt3 = f"平价{core}"
            for a in (alt2, alt3):
                if a not in results and len(results) < 3:
                    results.append(a)
    elif any(k in clean for k in ("教程", "如何", "制作", "秘诀", "方法", "说明", "练习", "教学")) or any(
        k in norm for k in ("cach", "huong dan", "cong thuc", "meo", "bai tap")
    ):
        core = re.sub(r"(制作|教程|说明|秘诀|方法|如何|教学)", "", clean)
        if core:
            alt2 = f"{core}教程"
            alt3 = f"新手{core}"
            for a in (alt2, alt3):
                if a not in results and len(results) < 3:
                    results.append(a)
    else:
        alt2 = f"沉浸式{clean}"
        alt3 = f"{clean}好物"
        for a in (alt2, alt3):
            if a not in results and len(results) < 3:
                results.append(a)

    return results[:3]


def extract_mandatory_attributes(norm: str) -> tuple[list[str], list[list[str]]]:
    """Trích xuất danh sách tên thuộc tính và danh sách các nhóm token (AND giữa các nhóm, OR trong nhóm)."""
    names: list[str] = []
    groups: list[list[str]] = []

    # 1. Season detection with disambiguation
    # Make sure 'dong' is not part of 'dong ho', 'dong xu', 'dong nat', etc.
    season_name = None
    season_tokens = None
    if "mua dong" in norm:
        season_name = "mua dong"
        season_tokens = ["冬季", "冬日", "冬"]
    elif re.search(r"\bdong\b", norm):
        is_watch_or_currency = bool(
            re.search(r"\bdong\s+(?:ho|xu|nat|hanh|nguoi|tien|bac|cua)\b", norm)
        )
        has_winter_context = bool(
            re.search(r"\b(?:phoi\s+do|ao|quan|vay|thoi\s+trang|troi|ret|lanh|am|mua)\b", norm)
        )
        if not is_watch_or_currency and has_winter_context:
            season_name = "mua dong"
            season_tokens = ["冬季", "冬日", "冬"]
    elif "mua he" in norm:
        season_name = "mua he"
        season_tokens = ["夏季", "夏日", "夏"]
    elif re.search(r"\bhe\b", norm):
        is_system = bool(re.search(r"\b(?:he\s+thong|the\s+he)\b", norm))
        has_summer_context = bool(
            re.search(r"\b(?:phoi\s+do|ao|quan|vay|troi|nong|mua)\b", norm)
        )
        if not is_system and has_summer_context:
            season_name = "mua he"
            season_tokens = ["夏季", "夏日", "夏"]
    elif "mua thu" in norm:
        season_name = "mua thu"
        season_tokens = ["秋季", "秋日", "秋"]
    elif re.search(r"\bthu\b", norm):
        is_recording = bool(
            re.search(r"\b(?:thu\s+(?:am|phi|thap|nghiem|nhan|phap|chi|thuat))\b", norm)
        )
        has_autumn_context = bool(
            re.search(r"\b(?:phoi\s+do|ao|quan|vay|troi|mua)\b", norm)
        )
        if not is_recording and has_autumn_context:
            season_name = "mua thu"
            season_tokens = ["秋季", "秋日", "秋"]
    elif "mua xuan" in norm:
        season_name = "mua xuan"
        season_tokens = ["春季", "春日", "春"]

    if season_name and season_tokens:
        names.append(season_name)
        groups.append(season_tokens)

    # 2. Price detection
    if "gia re" in norm or "binh dan" in norm:
        names.append("gia re")
        groups.append(["平价", "性价比", "百元"])

    # 3. Style detection
    if "vintage" in norm:
        names.append("vintage")
        groups.append(["复古"])
    elif "co dien" in norm:
        names.append("co dien")
        groups.append(["复古"])
    elif "y2k" in norm:
        names.append("y2k")
        groups.append(["Y2K"])

    # 4. Role detection
    if "hoc sinh" in norm:
        names.append("hoc sinh")
        groups.append(["学生"])
    elif "sinh vien" in norm:
        names.append("sinh vien")
        groups.append(["学生", "大学"])
    elif "di lam" in norm:
        names.append("di lam")
        groups.append(["通勤", "上班族"])
    elif "di hoc" in norm:
        names.append("di hoc")
        groups.append(["上学", "学生"])

    # 5. Core product / tool detection
    if "nhip" in norm or "tweezers" in norm:
        names.append("nhip")
        groups.append(["镊子", "鑷子"])

    return names, groups


def satisfies_all_attribute_groups(candidate: str, groups: list[list[str]]) -> bool:
    if not groups:
        return True
    return all(any(token in candidate for token in grp) for grp in groups)


async def fast_online_translate_async(text: str) -> str | None:
    if not text:
        return None
    cached = _ONLINE_TRANSLATE_CACHE.get(text)
    if cached:
        return cached
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text}
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                if data and isinstance(data, list) and len(data) > 0 and data[0]:
                    translated = "".join(segment[0] for segment in data[0] if segment and segment[0])
                    if translated:
                        res = translated.strip()
                        _ONLINE_TRANSLATE_CACHE[text] = res
                        return res
    except Exception as e:
        logger.debug("Fast translate async failed: %s", e)
    return None


def fast_online_translate(text: str) -> str | None:
    cached = _ONLINE_TRANSLATE_CACHE.get(text)
    if cached:
        return cached
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text}
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                if data and isinstance(data, list) and len(data) > 0 and data[0]:
                    translated = "".join(segment[0] for segment in data[0] if segment and segment[0])
                    if translated:
                        res = translated.strip()
                        _ONLINE_TRANSLATE_CACHE[text] = res
                        return res
    except Exception as e:
        logger.debug("Fast translate sync failed: %s", e)
    return None


# Tool + purpose must remain together; broad expansion must not erase either.
COMPOUND_INTENTS = [
    (["nhip", "nhip gap", "kep gap", "tweezers", "tweezer", "镊子", "鑷子"],
     ["sticker", "stickers", "hinh dan", "guka", "咕卡", "贴纸", "貼紙"],
     ["贴纸镊子", "咕卡贴纸镊子", "贴纸专用镊子"]),
    (["dung cu", "dung cu boc", "cay boc", "dao boc", "工具", "手账工具", "刮刀"],
     ["sticker", "stickers", "hinh dan", "guka", "咕卡", "贴纸", "貼紙"],
     ["贴纸镊子", "咕卡工具", "手账贴纸工具"]),
    (["nhip", "kep", "tweezers", "镊子", "鑷子"],
     ["mi", "eyelash", "睫毛"],
     ["睫毛镊子", "嫁接睫毛镊子", "睫毛专用镊子"]),
    (["khuon", "mould", "mold", "模具"],
     ["banh", "cake", "baking", "烘焙", "蛋糕"],
     ["烘焙模具", "蛋糕模具", "烘焙蛋糕模具"]),
    (["co", "brush", "刷"],
     ["trang diem", "makeup", "danh nen", "化妆", "底妆"],
     ["化妆刷", "粉底刷", "底妆刷"]),
    (["mut", "bong mut", "sponge", "puff", "粉扑", "美妆蛋"],
     ["trang diem", "makeup", "danh nen", "化妆", "底妆"],
     ["美妆蛋", "粉扑", "气垫粉扑"]),
    (["but", "pen", "笔"],
     ["nail", "ve mong", "mong tay", "美甲"],
     ["美甲彩绘笔", "美甲拉线笔", "美甲笔"]),
    (["giay", "paper", "纸"],
     ["goi hoa", "hoa", "花", "包花"],
     ["包花纸", "鲜花包装纸", "花束包装纸"]),
    (["hop dung", "hop", "ke", "box", "storage", "收纳盒", "收纳"],
     ["my pham", "son", "makeup", "cosmetics", "化妆品", "护肤品"],
     ["化妆品收纳盒", "护肤品收纳", "桌面化妆品收纳"]),
    (["binh", "ly", "cup", "bottle", "杯"],
     ["giu nhiet", "thermal", "insulated", "保温"],
     ["保温杯", "保温水杯", "大容量保温杯"]),
    (["tai nghe", "headphone", "earphone", "耳机"],
     ["bluetooth", "khong day", "wireless", "蓝牙", "无线"],
     ["蓝牙耳机", "无线蓝牙耳机", "降噪蓝牙耳机"]),
    (["chuot", "mouse", "鼠标"],
     ["khong day", "bluetooth", "wireless", "无线", "蓝牙"],
     ["无线鼠标", "蓝牙无线鼠标", "静音无线鼠标"]),
    (["day sac", "cap sac", "cable", "数据线", "充电线"],
     ["sac nhanh", "fast charge", "quick charge", "快充"],
     ["快充数据线", "快充电线", "快充线"]),
    (["may uon", "may lam", "curler", "卷发棒"],
     ["toc", "hair", "卷发", "发型"],
     ["卷发棒", "自动卷发棒", "电卷棒"]),
    (["may say", "dryer", "吹风机"],
     ["toc", "hair", "吹发", "头发"],
     ["吹风机", "高速吹风机", "负离子吹风机"]),
]


def compound_intent(query: str) -> tuple[list[list[str]], list[str]] | None:
    normalized = normalize_vietnamese(query)
    def matches(alias: str) -> bool:
        if is_cjk(alias):
            return alias in normalized
        return bool(re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", normalized))
    for tool, purpose, queries in COMPOUND_INTENTS:
        if any(matches(a) for a in tool) and any(matches(a) for a in purpose):
            return [tool, purpose], queries
    return None


def plan_query(query: str) -> QueryPlan:
    raw_query = query.strip()
    if is_cjk(raw_query):
        return QueryPlan(
            subject=raw_query,
            tiered_queries=[raw_query],
        )

    clean_query, raw_tags = extract_hashtag_intent(raw_query)
    target_query = clean_query if clean_query else raw_query

    norm = normalize_vietnamese(target_query)
    mandatory, groups = extract_mandatory_attributes(norm)
    subject = extract_subject(target_query, norm)
    action = extract_action(target_query, norm)

    modifiers: list[str] = []
    for key in sorted(MODIFIER_MAP.keys(), key=len, reverse=True):
        if re.search(r"\b" + re.escape(key) + r"\b", norm):
            modifiers.append(key)

    tiered = generate_tiered_queries(
        target_query,
        norm=norm,
        mandatory=mandatory,
        groups=groups,
        subject=subject,
        action=action,
        modifiers=modifiers,
    )

    if raw_tags and tiered:
        hashtag_tiered: list[str] = []
        for q in tiered:
            clean_q = q.lstrip("#")
            hashtag_q = f"#{clean_q}"
            if hashtag_q not in hashtag_tiered:
                hashtag_tiered.append(hashtag_q)
            if clean_q not in hashtag_tiered:
                hashtag_tiered.append(clean_q)
        tiered = hashtag_tiered[:3]

    return QueryPlan(
        subject=subject or target_query,
        action=action,
        mandatory_attributes=mandatory,
        modifiers=modifiers,
        tiered_queries=tiered,
    )


async def plan_query_async(query: str) -> QueryPlan:
    raw_query = query.strip()
    if is_cjk(raw_query):
        return QueryPlan(
            subject=raw_query,
            tiered_queries=[raw_query],
        )

    clean_query, raw_tags = extract_hashtag_intent(raw_query)
    target_query = clean_query if clean_query else raw_query

    norm = normalize_vietnamese(target_query)
    mandatory, groups = extract_mandatory_attributes(norm)
    subject = extract_subject(target_query, norm)
    action = extract_action(target_query, norm)

    modifiers: list[str] = []
    for key in sorted(MODIFIER_MAP.keys(), key=len, reverse=True):
        if re.search(r"\b" + re.escape(key) + r"\b", norm):
            modifiers.append(key)

    tiered = generate_tiered_queries(
        target_query,
        norm=norm,
        mandatory=mandatory,
        groups=groups,
        subject=subject,
        action=action,
        modifiers=modifiers,
    )

    if tiered == [target_query] and not is_cjk(target_query):
        restored = restore_vietnamese_phrases(target_query)
        online = await fast_online_translate_async(restored)
        if online:
            clean_online = re.sub(r"\s+", "", online)
            if any("\u4e00" <= c <= "\u9fff" for c in clean_online):
                tiered = synthesize_tiered_from_translation(clean_online, target_query)
                if not subject or subject == target_query:
                    subject = clean_online

    if raw_tags and tiered:
        hashtag_tiered: list[str] = []
        for q in tiered:
            clean_q = q.lstrip("#")
            hashtag_q = f"#{clean_q}"
            if hashtag_q not in hashtag_tiered:
                hashtag_tiered.append(hashtag_q)
            if clean_q not in hashtag_tiered:
                hashtag_tiered.append(clean_q)
        tiered = hashtag_tiered[:3]

    return QueryPlan(
        subject=subject or target_query,
        action=action,
        mandatory_attributes=mandatory,
        modifiers=modifiers,
        tiered_queries=tiered,
    )


def generate_tiered_queries(
    raw_query: str,
    norm: str | None = None,
    mandatory: list[str] | None = None,
    groups: list[list[str]] | None = None,
    subject: str | None = None,
    action: str | None = None,
    modifiers: list[str] | None = None,
) -> list[str]:
    if is_cjk(raw_query):
        return [raw_query.strip()]

    if norm is None:
        norm = normalize_vietnamese(raw_query)
    if mandatory is None or groups is None:
        mandatory, groups = extract_mandatory_attributes(norm)
    if subject is None:
        subject = extract_subject(raw_query, norm)
    if action is None:
        action = extract_action(raw_query, norm)
    if modifiers is None:
        modifiers = [
            k
            for k in sorted(MODIFIER_MAP.keys(), key=len, reverse=True)
            if re.search(r"\b" + re.escape(k) + r"\b", norm)
        ]

    compound = compound_intent(raw_query)
    if compound:
        _, precise_queries = compound
        action_terms = ACTION_MAP.get(action, []) if action else []
        suffix = action_terms[0] if action_terms else ""
        res: list[str] = []
        for term in precise_queries:
            prefix = "".join(grp[0] for grp in groups if not any(tok in term for tok in grp))
            res.append(prefix + term + suffix)
        return res

    candidates: list[str] = []

    subj_data = SUBJECT_MAP.get(subject, {}) if subject else {}
    subj_tokens = (subj_data.get("primary", []) + subj_data.get("natural", [])) if subj_data else []

    # 1. So khớp từ điển cụm từ chuyên sâu dài nhất trước
    for phrase, translations in sorted(
        PHRASE_DICTIONARY.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if re.search(r"\b" + re.escape(phrase) + r"\b", norm):
            if not is_valid_phrase_match(raw_query, norm, phrase):
                continue
            if subject and subject not in phrase and subj_tokens:
                if not any(any(st in term for st in subj_tokens) for term in translations):
                    continue
            for term in translations:
                # Nếu có thuộc tính bắt buộc, yêu cầu thoả mãn tất cả các nhóm (AND giữa các nhóm)
                if not satisfies_all_attribute_groups(term, groups):
                    continue
                if subject and subject not in phrase and subj_tokens and not any(st in term for st in subj_tokens):
                    continue
                if term not in candidates:
                    candidates.append(term)

    # 2. Ghép có cấu trúc theo 3 tầng nếu chưa đủ 3 query
    subj_data = SUBJECT_MAP.get(subject, {}) if subject else {}
    act_terms = ACTION_MAP.get(action, []) if action else []
    mod_terms = MODIFIER_MAP.get(modifiers[0], []) if modifiers else []

    s_prim = subj_data.get("primary", [""])[0] if subj_data else ""
    a_prim = act_terms[0] if act_terms else ""
    m_prim = mod_terms[0] if mod_terms else ""

    # Chỉ ghép có cấu trúc nếu có subject rõ ràng; tránh tạo từ chung chung rác (沉浸式, 少女心, 测评, 开箱) khi mất chủ đề
    if s_prim and len(candidates) < 3:
        mand_token = "".join(grp[0] for grp in groups)
        # Tầng 1: Primary (Chính xác cốt lõi: Mandatory + Modifier + Subject + Action)
        tier1 = f"{mand_token}{m_prim}{s_prim}{a_prim}".strip()
        if tier1 and tier1 not in candidates and satisfies_all_attribute_groups(tier1, groups):
            candidates.append(tier1)

        # Tầng 2: Natural (Biến thể tự nhiên, hot-trend)
        s_nat = subj_data.get("natural", [s_prim])[0] if subj_data else s_prim
        if s_nat or a_prim:
            price_tok = next((tok for grp in groups for tok in grp if tok in ("平价", "性价比", "百元")), "")
            has_winter = any(t in mand_token for t in ("冬季", "冬日", "冬"))
            if "开箱" in a_prim or "开箱" in s_nat:
                tier2 = f"沉浸式{mand_token}{s_nat}".strip()
            elif has_winter and price_tok:
                tier2 = f"冬日{price_tok}OOTD".strip()
            elif has_winter:
                tier2 = f"{mand_token}保暖{s_nat}".strip()
            elif mand_token:
                tier2 = f"{mand_token}{s_nat}{a_prim}".strip()
            else:
                tier2 = f"沉浸式{s_nat}{a_prim}".strip()
            if tier2 and tier2 not in candidates and satisfies_all_attribute_groups(tier2, groups):
                candidates.append(tier2)

        # Tầng 3: Narrow (Biến thể ngách: 学生 / OOTD / 少女心)
        s_nar = subj_data.get("narrow", [s_prim])[0] if subj_data else s_prim
        if s_nar or a_prim:
            price_tok = next((tok for grp in groups for tok in grp if tok in ("平价", "性价比", "百元")), "")
            has_winter = any(t in mand_token for t in ("冬季", "冬日", "冬"))
            if has_winter and price_tok:
                tier3 = f"冬季百元{s_nar}".strip()
            elif has_winter:
                tier3 = f"{mand_token}OOTD".strip()
            elif "开箱" in a_prim or "开箱" in s_nar:
                tier3 = f"学生{mand_token}{s_nar}".strip()
            elif mand_token:
                tier3 = f"学生{mand_token}{s_nar}".strip()
            else:
                tier3 = f"少女心{s_nar}".strip()
            if tier3 and tier3 not in candidates and satisfies_all_attribute_groups(tier3, groups):
                candidates.append(tier3)

    # 3. Bổ sung từ khóa đơn lẻ từ WORD_MAP nếu vẫn chưa đủ (chỉ khi có chủ đề rõ ràng)
    if len(candidates) < 3 and s_prim:
        for word, translations in WORD_MAP.items():
            if re.search(r"\b" + re.escape(word) + r"\b", norm):
                for trans in translations:
                    mand_token = "".join(grp[0] for grp in groups)
                    term = f"{mand_token}{trans}".strip()
                    if term not in candidates and satisfies_all_attribute_groups(term, groups):
                        candidates.append(term)
                    if len(candidates) >= 3:
                        break
            if len(candidates) >= 3:
                break

    # Đảm bảo giữ đúng ngân sách tối đa 3 truy vấn phân tầng
    final_terms: list[str] = []
    for item in candidates:
        cleaned = item.strip()
        if cleaned and cleaned not in final_terms:
            final_terms.append(cleaned)
        if len(final_terms) >= 3:
            break

    return final_terms if final_terms else [raw_query]


def translate_query(query: str) -> list[str]:
    """Tương thích ngược: trả về danh sách các truy vấn đã dịch cho query."""
    return generate_tiered_queries(query)
