const folderNames = [
    ['6890來億-KY', '6568宏觀', '3005神基', '6757台灣虎航', '4933友輝', '2441超豐'],
    ['3034聯詠', '6890來億-KY', '3081聯亞', '4772台特化', '2645長榮航太', '6206飛捷', '4987科誠']
]

const output = document.getElementById('output');
                    output.innerHTML  += '<br>'
                    for(var t = 0; t < folderNames[d].length; t++) {
                        output.innerHTML += '<option value=#pic_'+t+'_>'+ folderNames[d][t] +'</option>'
                        }