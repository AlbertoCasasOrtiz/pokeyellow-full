CeladonMansionRoofHouse_Script:
	call EnableAutoTextBoxDrawing
	ret

CeladonMansionRoofHouse_TextPointers:
	def_text_pointers
	dw_const CeladonMansionRoofHouseHikerText,         TEXT_CELADONMANSION_ROOF_HOUSE_HIKER
	dw_const CeladonMansionRoofHouseMewPokeballText, TEXT_CELADONMANSION_ROOF_HOUSE_MEW_POKEBALL

CeladonMansionRoofHouseHikerText:
	text_far _CeladonMansionRoofHouseHikerText
	text_end

CeladonMansionRoofHouseMewPokeballText:
	text_asm
	lb bc, MEW, 25
	call GivePokemon
	jr nc, .party_full
	ld a, TOGGLE_CELADON_MANSION_MEW_GIFT
	ld [wToggleableObjectIndex], a
	predef HideObject
.party_full
	jp TextScriptEnd
