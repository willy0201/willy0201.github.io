const folderNames = [
    ['6890來億-KY', '6568宏觀', '3005神基', '6757台灣虎航', '4933友輝', '2441超豐'],
    ['3034聯詠', '6890來億-KY', '3081聯亞', '4772台特化', '2645長榮航太', '6206飛捷', '4987科誠'],
    ['3563牧德','3044健鼎','3596智易','3147大綜','2480敦陽科','8436大江','3213茂訊','2412中華電','3045台灣大','1215卜蜂','4904遠傳','1216統一','4105東洋','6180橘子','6257矽格','2006東和鋼鐵','2387精元','6201亞弘電','6292迅德'],
    ['5314世紀','3563牧德','6469大樹','2753八方雲集','9921巨大','5439高技','8436大江','2439美律','6278台表科','2850新產','2421建準','2451創見','6877鏵友益','2211長榮鋼','3260威剛','3026禾伸堂','4931新盛力','1799易威','6658聯策','3528安馳','2535達欣工',],
    ['3034聯詠','3596智易','2474可成','6799來頡','4904遠傳','6203海韻電','6186新潤','2468華經'],
    ['6199天品', '6753龍德造船', '7402邑錡', '4904遠傳', '1784訊聯'],
    ['3264欣銓', '6228全譜', '2038海光']
]

const output = document.getElementById('output');
                    output.innerHTML  += '<br>'
                    for(var t = 0; t < folderNames[d].length; t++) {
                        output.innerHTML += '<option value=#pic_'+t+'_>'+ folderNames[d][t] +'</option>'
                        }