import {test, expect} from "@playwright/test";
test("landing selects real search; mock remains explicit",async({page})=>{
 await page.goto("/");
 await expect(page.getByRole("combobox",{name:"Nguồn tìm kiếm"})).toHaveValue("xiaohongshu");
 await page.goto("/?mode=mock");
 await expect(page.getByRole("combobox",{name:"Nguồn tìm kiếm"})).toHaveValue("mock");
});
