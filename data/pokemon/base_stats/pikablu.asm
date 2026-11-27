db DEX_PIKABLU ; pokedex id

db  70,  20,  50,  40,  50
;   hp  atk  def  spd  spc

db WATER, WATER ; type
db 45 ; catch rate
db 88 ; base exp

; Temporary graphics: Eevee placeholder
INCBIN "gfx/pokemon/front/pikablu.pic", 0, 1
dw PikabluPicFront, PikabluPicBack

db TAIL_WHIP, WATER_GUN, BUBBLE, NO_MOVE
db GROWTH_MEDIUM_FAST

; tm/hm learnset
tmhm TOXIC,        BODY_SLAM,    TAKE_DOWN,    DOUBLE_EDGE,  RAGE,         \
     MIMIC,        DOUBLE_TEAM,  REFLECT,      BIDE,         SWIFT,        \
     SKULL_BASH,   REST,         SUBSTITUTE,   SURF,         ICE_BEAM,     \
     BLIZZARD,     WATER_GUN,    BUBBLEBEAM
; end

db 0 ; padding
