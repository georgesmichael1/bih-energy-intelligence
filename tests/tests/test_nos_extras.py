import nos_extras as x


def test_power_mw_forms():
    assert x.power_mw('HPP Bočac, 2x55 MW') == 110
    assert x.power_mw('TPP Tuzla, 1x100+2x200+1x223 MW') == 723
    assert x.power_mw('SPP Gaj 1 and 2, 4,1028 + 3,4801 MW') == 7.5829
    assert x.power_mw('WPP Poklečani, 132MW') == 132
    assert x.power_mw('SPP MI SOLIS 1-36, 35,64 MW (power plants less than 1 MW)') == 35.64
    assert x.power_mw('BESS Zenica, 43,5 MW, 174 MWh') == 43.5
    # two different MW figures: left empty, never guessed
    assert x.power_mw('SPP Solario 1 and 2, 4 + 4 MW + 11, 4378 MW (power plants less than 1 MW)') is None
    assert x.power_mw('Aluminij Mostar') is None


def test_types_and_providers():
    assert x.plant_type('BESS Glinište, 43,2 MW', '') == 'Storage (BESS)'
    assert x.plant_type('Aluminij Mostar', 'electric power consumer') == 'Consumer'
    assert x.provider_name('EFT') == 'EFT Stanari'
    assert x.provider_name('Elektrane Stanari') == 'EFT Stanari'
    assert x.product_of('SR_off peak load') == 'aFRR_Night'
    assert x.product_of('aFRR_peak load') == 'aFRR_Day'
    assert x.product_of('TRDownward') == 'mFRR_Down'
    assert x.product_of('TR_Upward') == 'mFRR_Up'
    assert x.product_of('Bal.market') is None


def test_fnum_blank_and_text():
    assert x.fnum(None) is None and x.fnum('') is None and x.fnum('#REF!') is None
    assert x.fnum('14,45') == 14.45 and x.fnum(0) == 0.0
